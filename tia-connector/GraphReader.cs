using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Xml;
using System.Xml.Linq;

internal static class GraphReader
{
    private static IEnumerable<XElement> Children(XElement node, string name)
    { return node.Elements().Where(e => e.Name.LocalName == name); }
    private static XElement Child(XElement node, string name)
    { return Children(node, name).Single(); }
    private static string Attr(XElement node, string name)
    { return (string)node.Attribute(name); }

    public static object Read(string path, string stepNumber, string transitionNumber)
    {
        int number;
        if (!int.TryParse(stepNumber, out number) || !int.TryParse(transitionNumber, out number))
            throw new ArgumentException("Step and transition numbers must be integers.");
        XDocument document;
        using (var reader = XmlReader.Create(path, new XmlReaderSettings {
            DtdProcessing = DtdProcessing.Prohibit, XmlResolver = null, MaxCharactersInDocument = 50000000
        })) document = XDocument.Load(reader);
        var graph = document.Descendants().Where(e => e.Name.LocalName == "Graph").Single();
        var sequence = Child(graph, "Sequence");
        var step = Children(Child(sequence, "Steps"), "Step").SingleOrDefault(e => Attr(e, "Number") == stepNumber);
        var transition = Children(Child(sequence, "Transitions"), "Transition").SingleOrDefault(e => Attr(e, "Number") == transitionNumber);
        if (step == null || transition == null) throw new ArgumentException("Requested step or transition was not found.");
        var connections = Children(Child(sequence, "Connections"), "Connection").ToArray();
        bool linked = connections.Any(c => Child(c, "NodeFrom").Elements().Any(e => e.Name.LocalName == "StepRef" && Attr(e, "Number") == stepNumber)
            && Child(c, "NodeTo").Elements().Any(e => e.Name.LocalName == "TransitionRef" && Attr(e, "Number") == transitionNumber));
        if (!linked) throw new ArgumentException("The transition is not directly connected after this step.");
        string expression = null, unsupported = null;
        var net = Child(transition, "FlgNet");
        try { expression = new Network(net).Expression(); }
        catch (NotSupportedException ex) { unsupported = ex.Message; }
        return new {
            ok = true, source = Path.GetFullPath(path), liveValuesAvailable = false,
            step = new { number = stepNumber, name = Attr(step, "Name"),
                maximumStepTime = Attr(step, "MaximumStepTime"), warningTime = Attr(step, "WarningTime"),
                actions = Children(Child(step, "Actions"), "Action").Where(a => a.Elements().Any()).Select(a => new {
                    attributes = Attributes(a), text = string.Concat(Children(a, "Token").Select(t => Attr(t, "Text"))), structure = Tree(a)
                }).ToArray(),
                interlocks = Tree(Child(step, "Interlocks")), supervisions = Tree(Child(step, "Supervisions")) },
            transition = new { number = transitionNumber, name = Attr(transition, "Name"),
                expression = expression, expressionSupported = unsupported == null, unsupportedReason = unsupported,
                network = Tree(net),
                nextNodes = connections.Where(c => Child(c, "NodeFrom").Elements().Any(e => e.Name.LocalName == "TransitionRef" && Attr(e, "Number") == transitionNumber))
                    .Select(c => Tree(Child(c, "NodeTo"))).ToArray() }
        };
    }

    private static Dictionary<string, string> Attributes(XElement node)
    { return node.Attributes().Where(a => !a.IsNamespaceDeclaration).ToDictionary(a => a.Name.LocalName, a => a.Value); }
    private static object Tree(XElement node)
    { return new { kind = node.Name.LocalName, attributes = Attributes(node), text = node.HasElements ? null : node.Value, children = node.Elements().Select(Tree).ToArray() }; }

    // Decode only proven combinational primitives. Unknown instructions remain explicit.
    private sealed class Network
    {
        private readonly Dictionary<string, XElement> parts;
        private readonly XElement[] wires;
        private readonly HashSet<string> active = new HashSet<string>();
        public Network(XElement net)
        {
            parts = Child(net, "Parts").Elements().ToDictionary(e => Attr(e, "UId"));
            wires = Children(Child(net, "Wires"), "Wire").ToArray();
        }
        public string Expression()
        {
            var coil = parts.Values.Single(e => Attr(e, "Name") == "TrCoil");
            return Input(Attr(coil, "UId"), "in");
        }
        private string Input(string id, string port)
        {
            var wire = wires.Single(w => Children(w, "NameCon").Any(n => Attr(n, "UId") == id && Attr(n, "Name") == port));
            if (Children(wire, "Powerrail").Any()) return "TRUE";
            var ident = Children(wire, "IdentCon").SingleOrDefault();
            if (ident != null) return Operand(parts[Attr(ident, "UId")]);
            var source = Children(wire, "NameCon").Where(n => Attr(n, "Name") == "out").Single();
            return Output(Attr(source, "UId"));
        }
        private string Output(string id)
        {
            if (!active.Add(id)) throw new NotSupportedException("Cyclic network.");
            try
            {
                var part = parts[id]; var name = Attr(part, "Name");
                if (name == "Contact")
                {
                    var operand = Input(id, "operand");
                    if (Children(part, "Negated").Any(n => Attr(n, "Name") == "operand")) operand = "NOT(" + operand + ")";
                    var before = Input(id, "in");
                    return before == "TRUE" ? operand : "(" + before + " AND " + operand + ")";
                }
                if (name == "O")
                {
                    int count = int.Parse(Children(part, "TemplateValue").Single(t => Attr(t, "Name") == "Card").Value);
                    return "(" + string.Join(" OR ", Enumerable.Range(1, count).Select(i => Input(id, "in" + i))) + ")";
                }
                throw new NotSupportedException("Unsupported instruction: " + name);
            }
            finally { active.Remove(id); }
        }
        private string Operand(XElement access)
        {
            if (Attr(access, "Scope") != "GlobalVariable" && Attr(access, "Scope") != "LocalVariable")
                throw new NotSupportedException("Unsupported operand scope: " + Attr(access, "Scope"));
            return (Attr(access, "Scope") == "LocalVariable" ? "#" : "") + string.Join(".", Child(access, "Symbol").Elements().Select(c => {
                var name = "\"" + Attr(c, "Name") + "\"";
                if (Attr(c, "AccessModifier") == "Array")
                {
                    var indices = c.Elements().Select(a => {
                        if (Attr(a, "Scope") != "LiteralConstant") throw new NotSupportedException("Dynamic array index.");
                        return Child(Child(a, "Constant"), "ConstantValue").Value;
                    });
                    name += "[" + string.Join(",", indices) + "]";
                }
                return name;
            }));
        }
    }
}
