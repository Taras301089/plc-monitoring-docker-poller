using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Web.Script.Serialization;
using Siemens.Engineering;
using Siemens.Engineering.HW;
using Siemens.Engineering.HW.Features;
using Siemens.Engineering.SW;
using Siemens.Engineering.SW.Blocks;
using Siemens.Engineering.SW.Tags;
using Siemens.Engineering.SW.Types;

// Offline analysis helper: attach to an exact open project, or open the exact
// file as Secondary in an owned headless session. No save/compile/download/online.
internal static class ReadProject
{
    private static readonly string ApiDirectory = Environment.GetEnvironmentVariable("TIA_V17_API")
        ?? @"C:\Program Files\Siemens\Automation\Portal V17\PublicAPI\V17";
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = int.MaxValue };

    static ReadProject()
    {
        AppDomain.CurrentDomain.AssemblyResolve += delegate(object sender, ResolveEventArgs args)
        {
            var name = new AssemblyName(args.Name);
            if (!name.Name.StartsWith("Siemens.Engineering", StringComparison.Ordinal)) return null;
            var path = Path.Combine(ApiDirectory, name.Name + ".dll");
            if (!File.Exists(path)) return null;
            var assembly = Assembly.LoadFrom(path);
            if (assembly.GetName().FullName != name.FullName)
                throw new InvalidOperationException("Installed TIA API does not match the helper.");
            return assembly;
        };
    }

    public static int Main(string[] args)
    {
        try
        {
            if (args.Length == 1 && args[0] == "processes")
            {
                // Do not Dispose TiaPortalProcess: that API closes the process.
                Write(new { ok = true, processes = TiaPortal.GetProcesses().Select(p => new {
                    id = p.Id, projectPath = p.ProjectPath == null ? null : p.ProjectPath.FullName,
                    mode = p.Mode.ToString()
                }).ToArray() });
                return 0;
            }
            if (args.Length != 3 || (args[0] != "snapshot" && args[0] != "snapshot-file"))
                throw new ArgumentException("Usage: processes OR snapshot|snapshot-file <exact project.ap17> <new workspace output directory>");
            Snapshot(args[1], args[2], args[0] == "snapshot-file");
            return 0;
        }
        catch (Exception ex)
        {
            Write(new { ok = false, error = ex.Message, errorType = ex.GetType().Name });
            return 1;
        }
    }

    private static void Snapshot(string projectArgument, string outputArgument, bool openFile)
    {
        var projectPath = Path.GetFullPath(projectArgument);
        if (!File.Exists(projectPath) || !projectPath.EndsWith(".ap17", StringComparison.OrdinalIgnoreCase))
            throw new ArgumentException("An existing exact V17 project.ap17 path is required.");
        var output = Path.GetFullPath(outputArgument).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        var workspace = Directory.GetParent(AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar)).Parent.Parent.FullName;
        if (!output.StartsWith(workspace + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
            throw new ArgumentException("Output directory must be inside this helper's repository workspace.");
        if (Directory.Exists(output) || File.Exists(output))
            throw new ArgumentException("Output directory must not already exist.");

        var processes = TiaPortal.GetProcesses().Where(p => p.ProjectPath != null && SamePath(p.ProjectPath.FullName, projectPath)).ToList();
        if (!openFile && processes.Count != 1)
            throw new InvalidOperationException("Exactly one already-open TIA process must match the exact project path; found " + processes.Count + ".");

        Console.Error.WriteLine(openFile ? "Starting separate TIA session to read project file." : "Attaching to TIA process " + processes[0].Id + ".");
        // Dispose only our attachment/session; never dispose TiaPortalProcess.
        using (var portal = openFile ? new TiaPortal(TiaPortalMode.WithoutUserInterface) : processes[0].Attach())
        {
            var projects = openFile ? new List<Project> { portal.Projects.Open(new FileInfo(projectPath), null, ProjectOpenMode.Secondary) }
                : portal.Projects.Where(p => p.Path != null && SamePath(p.Path.FullName, projectPath)).ToList();
            if (projects.Count != 1)
                throw new InvalidOperationException("Attached project path mismatch: " + Json.Serialize(portal.Projects.Select(p => new { name = p.Name, path = p.Path == null ? null : p.Path.FullName }).ToArray()));
            var project = projects[0];
            Console.Error.WriteLine("Project ready: " + project.Name + ".");
            Directory.CreateDirectory(output);
            var started = DateTime.UtcNow.ToString("o");
            var records = new List<ExportRecord>();
            var plcs = Devices(project).SelectMany(d => Software(d.DeviceItems)).ToList();
            int ordinal = 0;
            var manifestPath = Path.Combine(output, "manifest.json");
            Action checkpoint = delegate {
                File.WriteAllText(manifestPath, Json.Serialize(new {
                    ok = true, project = projectPath, processId = openFile ? (int?)null : processes[0].Id,
                    startedAtUtc = started, lastUpdatedAtUtc = DateTime.UtcNow.ToString("o"),
                    scope = "ordinary PLC block/type/tag-table groups; no software units or hardware export",
                    plcs = plcs.Select(p => p.Name).ToArray(), count = records.Count,
                    failures = records.Count(r => !r.ok), exports = records
                }), new UTF8Encoding(false));
            };
            checkpoint();
            foreach (var plc in plcs)
            {
                Console.Error.WriteLine("Reading PLC " + plc.Name + ".");
                foreach (var item in Blocks(plc.BlockGroup, ""))
                {
                    var block = item.Item1;
                    var record = new ExportRecord { plc = plc.Name, kind = "block", groupPath = item.Item2, name = block.Name,
                        number = block.Number, language = block.ProgrammingLanguage.ToString() };
                    Export(record, output, ++ordinal, delegate(FileInfo file) { block.Export(file, ExportOptions.WithDefaults); });
                    records.Add(record);
                    if (ordinal % 20 == 0) { checkpoint(); Console.Error.WriteLine("Exported/attempted " + ordinal + " objects; failures " + records.Count(r => !r.ok) + "."); }
                }
                foreach (var item in Types(plc.TypeGroup, ""))
                {
                    var type = item.Item1;
                    var record = new ExportRecord { plc = plc.Name, kind = "type", groupPath = item.Item2, name = type.Name };
                    Export(record, output, ++ordinal, delegate(FileInfo file) { type.Export(file, ExportOptions.WithDefaults); });
                    records.Add(record);
                    if (ordinal % 20 == 0) { checkpoint(); Console.Error.WriteLine("Exported/attempted " + ordinal + " objects."); }
                }
                foreach (var item in TagTables(plc.TagTableGroup, ""))
                {
                    var table = item.Item1;
                    var record = new ExportRecord { plc = plc.Name, kind = "tagTable", groupPath = item.Item2, name = table.Name };
                    Export(record, output, ++ordinal, delegate(FileInfo file) { table.Export(file, ExportOptions.WithDefaults); });
                    records.Add(record);
                    if (ordinal % 20 == 0) { checkpoint(); Console.Error.WriteLine("Exported/attempted " + ordinal + " objects."); }
                }
            }
            checkpoint();
            Write(new { ok = true, project = projectPath, output = output, manifest = manifestPath,
                count = records.Count, failures = records.Count(r => !r.ok), completedAtUtc = DateTime.UtcNow.ToString("o") });
        }
    }

    private static void Export(ExportRecord record, string output, int ordinal, Action<FileInfo> export)
    {
        var name = new string(record.name.Select(c => Path.GetInvalidFileNameChars().Contains(c) ? '_' : c).ToArray());
        if (name.Length > 90) name = name.Substring(0, 90);
        record.filename = ordinal.ToString("D5") + "_" + record.kind + "_" + name + ".xml";
        try
        {
            var file = new FileInfo(Path.Combine(output, record.filename));
            export(file);
            file.Refresh();
            if (!file.Exists || file.Length == 0) throw new IOException("Export did not produce a nonempty XML.");
            record.ok = true;
        }
        catch (Exception ex)
        {
            record.error = ex.Message;
            record.errorType = ex.GetType().Name;
            Console.Error.WriteLine("Export failed: " + record.kind + " " + record.name + ": " + ex.Message);
        }
    }

    private static bool SamePath(string a, string b) { return string.Equals(Path.GetFullPath(a), Path.GetFullPath(b), StringComparison.OrdinalIgnoreCase); }
    private static string GroupPath(string parent, string name) { return parent.Length == 0 ? name : parent + "/" + name; }
    private static IEnumerable<Device> Devices(Project project)
    {
        foreach (var d in project.Devices) yield return d;
        foreach (var d in project.UngroupedDevicesGroup.Devices) yield return d;
        foreach (var g in project.DeviceGroups) foreach (var d in DevicesInGroup(g)) yield return d;
    }
    private static IEnumerable<Device> DevicesInGroup(DeviceUserGroup group)
    {
        foreach (var d in group.Devices) yield return d;
        foreach (var g in group.Groups) foreach (var d in DevicesInGroup(g)) yield return d;
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
    private static IEnumerable<Tuple<PlcBlock, string>> Blocks(PlcBlockGroup group, string parent)
    {
        var path = GroupPath(parent, group.Name);
        foreach (var block in group.Blocks) yield return Tuple.Create(block, path);
        foreach (var child in group.Groups) foreach (var block in Blocks(child, path)) yield return block;
    }
    private static IEnumerable<Tuple<PlcType, string>> Types(PlcTypeGroup group, string parent)
    {
        var path = GroupPath(parent, group.Name);
        foreach (var type in group.Types) yield return Tuple.Create(type, path);
        foreach (var child in group.Groups) foreach (var type in Types(child, path)) yield return type;
    }
    private static IEnumerable<Tuple<PlcTagTable, string>> TagTables(PlcTagTableGroup group, string parent)
    {
        var path = GroupPath(parent, group.Name);
        foreach (var table in group.TagTables) yield return Tuple.Create(table, path);
        foreach (var child in group.Groups) foreach (var table in TagTables(child, path)) yield return table;
    }
    private static void Write(object value) { Console.WriteLine(Json.Serialize(value)); }

    public sealed class ExportRecord
    {
        public bool ok;
        public string plc;
        public string kind;
        public string groupPath;
        public string name;
        public int? number;
        public string language;
        public string filename;
        public string error;
        public string errorType;
    }
}
