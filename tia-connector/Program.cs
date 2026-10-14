using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Web.Script.Serialization;
using Siemens.Engineering;
using Siemens.Engineering.HW;
using Siemens.Engineering.HW.Features;
using Siemens.Engineering.SW;
using Siemens.Engineering.SW.Blocks;

internal static class Program
{
    private static readonly string ApiDirectory = Environment.GetEnvironmentVariable("TIA_V17_API")
        ?? @"C:\Program Files\Siemens\Automation\Portal V17\PublicAPI\V17";

    static Program()
    {
        AppDomain.CurrentDomain.AssemblyResolve += delegate(object sender, ResolveEventArgs args)
        {
            var name = new AssemblyName(args.Name);
            if (!name.Name.StartsWith("Siemens.Engineering", StringComparison.Ordinal)) return null;
            var path = Path.Combine(ApiDirectory, name.Name + ".dll");
            if (!File.Exists(path)) return null;
            var assembly = Assembly.LoadFrom(path);
            if (assembly.GetName().FullName != name.FullName)
                throw new InvalidOperationException("Installed TIA API does not match the compiled connector.");
            return assembly;
        };
    }

    public static int Main(string[] args)
    {
        try
        {
            if (args.Length > 0 && args[0] == "inspect")
            {
                if (args.Length != 4) throw new ArgumentException("Usage: inspect <export.xml> <step number> <transition number>");
                Write(GraphReader.Read(args[1], args[2], args[3]));
                return 0;
            }
            if (args.Length == 1 && args[0] == "self-test")
            {
                Write(new { ok = true, apiPresent = File.Exists(Path.Combine(ApiDirectory, "Siemens.Engineering.dll")), version = "V17" });
                return 0;
            }
            if (args.Length < 2 || (args[0] != "list" && args[0] != "export"))
                throw new ArgumentException("Usage: list <project.ap17> OR export <project.ap17> <PLC name> <block name> <new output.xml>");
            if (args[0] == "list" && args.Length != 2 || args[0] == "export" && args.Length != 5)
                throw new ArgumentException("Incorrect argument count.");
            string projectPath = Path.GetFullPath(args[1]);
            if (!File.Exists(projectPath) || !projectPath.EndsWith(".ap17", StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("An existing V17 .ap17 project is required.");
            if (args[0] == "export")
            {
                var output = Path.GetFullPath(args[4]);
                if (!output.EndsWith(".xml", StringComparison.OrdinalIgnoreCase) || File.Exists(output))
                    throw new ArgumentException("Output must be a new .xml file; overwriting is disabled.");
                if (!Directory.Exists(Path.GetDirectoryName(output)))
                    throw new ArgumentException("Create the output directory before exporting.");
            }
            Run(args, projectPath);
            return 0;
        }
        catch (Exception ex)
        {
            Write(new { ok = false, error = ex.Message, errorType = ex.GetType().Name });
            return 1;
        }
    }

    private static void Run(string[] args, string projectPath)
    {
        // Only own a new session. Never attach to or close the engineer's open session.
        using (var portal = new TiaPortal(TiaPortalMode.WithoutUserInterface))
        {
            var project = portal.Projects.Open(new FileInfo(projectPath));
            var plcs = Devices(project).SelectMany(d => Software(d.DeviceItems)).ToList();
            if (args[0] == "list")
            {
                Write(new { ok = true, project = projectPath, plcs = plcs.Select(p => new {
                    name = p.Name, blocks = Blocks(p.BlockGroup).Select(b => new {
                        name = b.Name, number = b.Number, language = b.ProgrammingLanguage.ToString()
                    }).ToArray()
                }).ToArray() });
                return;
            }
            var matches = plcs.Where(p => p.Name == args[2]).ToList();
            if (matches.Count != 1) throw new InvalidOperationException("PLC name must match exactly one PLC; use list first.");
            var blocks = Blocks(matches[0].BlockGroup).Where(b => b.Name == args[3]).ToList();
            if (blocks.Count != 1) throw new InvalidOperationException("Block name must match exactly one block.");
            var output = new FileInfo(Path.GetFullPath(args[4]));
            blocks[0].Export(output, ExportOptions.WithDefaults);
            if (!File.Exists(output.FullName) || new FileInfo(output.FullName).Length == 0)
                throw new IOException("Export did not produce a nonempty file.");
            Write(new { ok = true, project = projectPath, plc = args[2], block = args[3], output = output.FullName,
                exportedAtUtc = DateTime.UtcNow.ToString("o") });
            // No save, compile, online connection, download or project mutation.
        }
    }

    private static IEnumerable<Device> Devices(Project project)
    {
        foreach (var d in project.Devices) yield return d;
        foreach (var d in project.UngroupedDevicesGroup.Devices) yield return d;
        foreach (var g in project.DeviceGroups)
            foreach (var d in DevicesInGroup(g)) yield return d;
    }

    private static IEnumerable<Device> DevicesInGroup(DeviceUserGroup group)
    {
        foreach (var d in group.Devices) yield return d;
        foreach (var g in group.Groups)
            foreach (var d in DevicesInGroup(g)) yield return d;
    }

    private static IEnumerable<PlcSoftware> Software(DeviceItemComposition items)
    {
        foreach (var item in items)
        {
            var container = item.GetService<SoftwareContainer>();
            var plc = container == null ? null : container.Software as PlcSoftware;
            if (plc != null) yield return plc;
            foreach (var child in Software(item.DeviceItems)) yield return child;
        }
    }

    private static IEnumerable<PlcBlock> Blocks(PlcBlockGroup group)
    {
        foreach (var block in group.Blocks) yield return block;
        foreach (var child in group.Groups)
            foreach (var block in Blocks(child)) yield return block;
    }

    private static void Write(object value)
    {
        Console.WriteLine(new JavaScriptSerializer { MaxJsonLength = int.MaxValue }.Serialize(value));
    }
}
