// lit's .NET program map. For each test class in a test assembly, it walks the IL of every method that class can
// reach across the repository's own assemblies, and hashes what it found. Two builds whose hash for a class is the
// same run that class's tests through the same code, so a class that passed on one passes on the other, apart from
// what the hash cannot see: external packages, files read at run time and the runtime itself, which the caller
// (dotnet_units.py) hashes separately.
//
// Reachability is conservative, never precise:
// - a method's direct calls, delegate targets (ldftn) and constructors are reached;
// - a type the code can create (newobj, initobj, box, newarr, a local's type) has every virtual method reached, so
//   any override a call could dispatch to is counted;
// - a type named only as data (typeof, a generic argument, an attribute argument, a fixture interface) is reached
//   whole, every method, because reflection, serializers and dependency injection reach its members by name;
// - touching a type at all reaches its static constructor and its shape (fields, attributes, base, interfaces);
// - code that looks types up by string (Type.GetType, Assembly.GetTypes, ...) makes its class "dynamic": its key
//   then covers every own assembly whole;
// - code that reflects over members (GetMethod, GetProperties, ...) reaches every created type whole.
//
// Usage: LitIlMap --tests <test.dll> --own <a.dll> [--own <b.dll> ...] [--content <basename> ...] --out <map.json>
using System.Collections.Immutable;
using System.Reflection;
using System.Reflection.Emit;
using System.Reflection.Metadata;
using System.Reflection.Metadata.Ecma335;
using System.Reflection.PortableExecutable;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

static class Program
{
    static int Main(string[] args)
    {
        var own = new List<string>();
        var content = new List<string>();
        string? tests = null, output = null;
        for (int i = 0; i < args.Length; i++)
        {
            string Next() => i + 1 < args.Length ? args[++i] : throw new ArgumentException($"{args[i]} needs a value");
            switch (args[i])
            {
                case "--own": own.Add(Next()); break;
                case "--tests": tests = Next(); break;
                case "--content": content.Add(Next()); break;
                case "--out": output = Next(); break;
                default: Console.Error.WriteLine($"Unknown argument {args[i]}"); return 2;
            }
        }
        if (tests is null || output is null)
        {
            Console.Error.WriteLine("Usage: LitIlMap --tests <test.dll> --own <a.dll> ... [--content <name> ...] --out <map.json>");
            return 2;
        }
        if (!own.Contains(tests)) own.Insert(0, tests);
        var map = new ProgramMap(own.Distinct().Select(Path.GetFullPath).ToList());
        var result = map.Units(Path.GetFullPath(tests), content.ToHashSet(StringComparer.Ordinal));
        File.WriteAllText(output, JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = false }));
        return 0;
    }
}

sealed class Module
{
    public required string Path;
    public required string Name;
    public required PEReader Pe;
    public required MetadataReader Md;
    public required string ResourceHash;
    public required string WholeHash;
}

sealed class MethodInfoRec
{
    public required string Key;
    public required string Type;
    public bool Virtual, Ctor, Cctor;
    public string Hash = "";
    public readonly List<string> Calls = new();
    public readonly List<string> Created = new();
    public readonly List<string> Whole = new();
    public readonly List<string> Touched = new();
    public readonly List<string> Strings = new();
    public bool Dynamic, Reflects, Resources;
}

sealed class TypeRec
{
    public required string Name;
    public required Module Module;
    public required TypeDefinitionHandle Handle;
    public string? Base;
    public string Shape = "";
    public readonly List<string> Methods = new();
    public readonly List<string> Nested = new();
    public readonly List<string> Whole = new();   // types named by its attributes, interfaces and base type arguments
    public readonly List<string> Members = new(); // own types of its fields and properties (reached by serializers)
    public readonly List<string> AttributeCtors = new();
    public bool IsTestClass, IsAbstract;
    public bool UsesCollection;
    public bool IsCollectionDefinition;
}

sealed class ProgramMap
{
    static readonly Dictionary<short, OpCode> OpCodesByValue = typeof(OpCodes)
        .GetFields(BindingFlags.Public | BindingFlags.Static)
        .Select(f => (OpCode)f.GetValue(null)!)
        .ToDictionary(o => o.Value);

    // String-driven type lookups: the code can reach any type, so the class depends on every own assembly.
    static readonly string[] DynamicApis =
    {
        "System.Type::GetType", "System.Reflection.Assembly::GetType", "System.Reflection.Assembly::GetTypes",
        "System.Reflection.Assembly::GetExportedTypes", "System.Reflection.Assembly::get_DefinedTypes",
        "System.Reflection.Assembly::get_ExportedTypes", "System.Reflection.Assembly::CreateInstance",
        "System.Reflection.Assembly::Load", "System.Reflection.Assembly::LoadFrom", "System.Reflection.Assembly::LoadFile",
        "System.AppDomain::GetAssemblies", "System.Runtime.Loader.AssemblyLoadContext::LoadFromAssemblyPath",
        "System.Runtime.Loader.AssemblyLoadContext::LoadFromAssemblyName", "System.Reflection.Module::GetTypes",
    };
    static readonly string[] ReflectApis =
    {
        "System.Type::GetMethod", "System.Type::GetMethods", "System.Type::GetProperty", "System.Type::GetProperties",
        "System.Type::GetField", "System.Type::GetFields", "System.Type::GetMember", "System.Type::GetMembers",
        "System.Type::GetConstructor", "System.Type::GetConstructors", "System.Type::InvokeMember",
        "System.Type::GetNestedType", "System.Type::GetNestedTypes", "System.Type::GetEvent", "System.Type::GetEvents",
        "System.Type::GetInterface", "System.Type::GetInterfaces", "System.Activator::CreateInstance",
        "System.Reflection.TypeInfo::get_DeclaredMethods", "System.Reflection.TypeInfo::get_DeclaredProperties",
        "System.Reflection.TypeInfo::get_DeclaredFields", "System.Reflection.TypeInfo::get_DeclaredMembers",
        "System.Reflection.TypeInfo::get_DeclaredConstructors",
    };
    static readonly string[] ResourceApis =
    {
        "System.Reflection.Assembly::GetManifestResourceStream", "System.Reflection.Assembly::GetManifestResourceNames",
        "System.Reflection.Assembly::GetManifestResourceInfo", "System.Resources.ResourceManager::.ctor",
    };

    readonly List<Module> modules = new();
    readonly Dictionary<string, TypeRec> types = new(StringComparer.Ordinal);
    readonly Dictionary<string, MethodInfoRec> methods = new(StringComparer.Ordinal);
    readonly string resources;
    readonly string wholeProgram;

    public ProgramMap(List<string> paths)
    {
        foreach (var path in paths)
        {
            var bytes = File.ReadAllBytes(path);
            var pe = new PEReader(ImmutableArray.Create(bytes));
            var md = pe.GetMetadataReader();
            var res = new StringBuilder();
            foreach (var h in md.ManifestResources)
            {
                var r = md.GetManifestResource(h);
                res.Append(md.GetString(r.Name)).Append('=');
                if (r.Implementation.IsNil)
                {
                    var section = pe.GetSectionData(pe.PEHeaders.CorHeader!.ResourcesDirectory.RelativeVirtualAddress);
                    var reader = section.GetReader((int)r.Offset, section.Length - (int)r.Offset);
                    int length = reader.ReadInt32();
                    res.Append(Sha(reader.ReadBytes(length)));
                }
                res.Append('\n');
            }
            modules.Add(new Module
            {
                Path = path,
                Name = md.GetString(md.GetAssemblyDefinition().Name),
                Pe = pe,
                Md = md,
                ResourceHash = Sha(Encoding.UTF8.GetBytes(res.ToString())),
                WholeHash = "",
            });
        }
        foreach (var m in modules) IndexTypes(m);
        foreach (var m in modules) IndexMembers(m);
        resources = Sha(string.Join("\n", modules.Select(m => m.Name + "=" + m.ResourceHash)));
        // The whole program, as seen by a dynamic class: every method hash and every type shape.
        wholeProgram = Sha(string.Join("\n",
            types.Values.Select(t => t.Name + "=" + t.Shape)
                .Concat(methods.Values.Select(x => x.Key + "=" + x.Hash))
                .OrderBy(s => s, StringComparer.Ordinal)) + "\n" + resources);
    }

    static string Sha(byte[] data) => Convert.ToHexString(SHA256.HashData(data)).ToLowerInvariant();
    static string Sha(string text) => Sha(Encoding.UTF8.GetBytes(text));

    // ---- names ----

    static string TypeDefName(MetadataReader md, TypeDefinitionHandle h)
    {
        var t = md.GetTypeDefinition(h);
        var name = md.GetString(t.Name);
        var declaring = t.GetDeclaringType();
        if (!declaring.IsNil) return TypeDefName(md, declaring) + "+" + name;
        var ns = md.GetString(t.Namespace);
        return ns.Length == 0 ? name : ns + "." + name;
    }

    static string TypeRefName(MetadataReader md, TypeReferenceHandle h)
    {
        var t = md.GetTypeReference(h);
        var name = md.GetString(t.Name);
        if (t.ResolutionScope.Kind == HandleKind.TypeReference)
            return TypeRefName(md, (TypeReferenceHandle)t.ResolutionScope) + "+" + name;
        var ns = md.GetString(t.Namespace);
        return ns.Length == 0 ? name : ns + "." + name;
    }

    // The definition a type handle names (generic instantiations by their generic definition), and every type
    // named inside it (generic arguments), so the caller can count those as reached whole.
    string TypeHandleName(Module m, EntityHandle h, List<string>? named)
    {
        switch (h.Kind)
        {
            case HandleKind.TypeDefinition: return TypeDefName(m.Md, (TypeDefinitionHandle)h);
            case HandleKind.TypeReference: return TypeRefName(m.Md, (TypeReferenceHandle)h);
            case HandleKind.TypeSpecification:
                var provider = new SigNames(this, m, named);
                var spec = m.Md.GetTypeSpecification((TypeSpecificationHandle)h);
                var decoded = spec.DecodeSignature(provider, null);
                return provider.Definition ?? decoded;
            default: return "?";
        }
    }

    sealed class SigNames : ISignatureTypeProvider<string, object?>
    {
        readonly ProgramMap map;
        readonly Module m;
        readonly List<string>? named;
        public string? Definition; // the outermost generic definition, set for a TypeSpec of a generic instantiation
        int depth;

        public SigNames(ProgramMap map, Module m, List<string>? named) { this.map = map; this.m = m; this.named = named; }

        string Note(string name) { if (named != null && map.types.ContainsKey(name)) named.Add(name); return name; }
        public string GetArrayType(string e, ArrayShape s) => e + "[" + new string(',', s.Rank - 1) + "]";
        public string GetByReferenceType(string e) => e + "&";
        public string GetFunctionPointerType(MethodSignature<string> s) => "fnptr(" + string.Join(",", s.ParameterTypes) + ")" + s.ReturnType;
        public string GetGenericInstantiation(string g, ImmutableArray<string> a)
        {
            if (depth == 0 && Definition is null) Definition = g;
            return g + "<" + string.Join(",", a) + ">";
        }
        public string GetGenericMethodParameter(object? c, int i) => "!!" + i;
        public string GetGenericTypeParameter(object? c, int i) => "!" + i;
        public string GetModifiedType(string mod, string u, bool req) => u;
        public string GetPinnedType(string e) => e;
        public string GetPointerType(string e) => e + "*";
        public string GetPrimitiveType(PrimitiveTypeCode t) => t.ToString();
        public string GetSZArrayType(string e) => e + "[]";
        public string GetTypeFromDefinition(MetadataReader r, TypeDefinitionHandle h, byte k) => Note(TypeDefName(r, h));
        public string GetTypeFromReference(MetadataReader r, TypeReferenceHandle h, byte k) => Note(TypeRefName(r, h));
        public string GetTypeFromSpecification(MetadataReader r, object? c, TypeSpecificationHandle h, byte k)
        {
            depth++;
            try { return r.GetTypeSpecification(h).DecodeSignature(this, c); }
            finally { depth--; }
        }
    }

    string MethodDefKey(Module m, MethodDefinitionHandle h)
    {
        var md = m.Md;
        var def = md.GetMethodDefinition(h);
        var sig = def.DecodeSignature(new SigNames(this, m, null), null);
        var arity = sig.GenericParameterCount > 0 ? "`" + sig.GenericParameterCount : "";
        return TypeDefName(md, def.GetDeclaringType()) + "::" + md.GetString(def.Name) + arity +
               "(" + string.Join(",", sig.ParameterTypes) + ")" + sig.ReturnType;
    }

    // A method token as a key: own methods resolve to their definition's key; others only need a readable name.
    string MethodTokenKey(Module m, EntityHandle h, List<string> named, out string simple)
    {
        var md = m.Md;
        switch (h.Kind)
        {
            case HandleKind.MethodDefinition:
            {
                var def = md.GetMethodDefinition((MethodDefinitionHandle)h);
                simple = TypeDefName(md, def.GetDeclaringType()) + "::" + md.GetString(def.Name);
                return MethodDefKey(m, (MethodDefinitionHandle)h);
            }
            case HandleKind.MemberReference:
            {
                var r = md.GetMemberReference((MemberReferenceHandle)h);
                string owner = r.Parent.Kind switch
                {
                    HandleKind.MethodDefinition => TypeDefName(md, md.GetMethodDefinition((MethodDefinitionHandle)r.Parent).GetDeclaringType()),
                    HandleKind.ModuleReference => "<module>",
                    _ => TypeHandleName(m, (EntityHandle)r.Parent, named),
                };
                var name = md.GetString(r.Name);
                simple = owner + "::" + name;
                if (r.GetKind() != MemberReferenceKind.Method) return simple;
                var sig = r.DecodeMethodSignature(new SigNames(this, m, named), null);
                var arity = sig.GenericParameterCount > 0 ? "`" + sig.GenericParameterCount : "";
                return owner + "::" + name + arity + "(" + string.Join(",", sig.ParameterTypes) + ")" + sig.ReturnType;
            }
            case HandleKind.MethodSpecification:
            {
                var spec = md.GetMethodSpecification((MethodSpecificationHandle)h);
                spec.DecodeSignature(new SigNames(this, m, named), null); // generic arguments count as named
                return MethodTokenKey(m, spec.Method, named, out simple);
            }
            default:
                simple = "?";
                return "?";
        }
    }

    string FieldTokenName(Module m, EntityHandle h, List<string> named, out string owner)
    {
        var md = m.Md;
        if (h.Kind == HandleKind.FieldDefinition)
        {
            var f = md.GetFieldDefinition((FieldDefinitionHandle)h);
            owner = TypeDefName(md, f.GetDeclaringType());
            return owner + "::" + md.GetString(f.Name);
        }
        var r = md.GetMemberReference((MemberReferenceHandle)h);
        owner = r.Parent.Kind == HandleKind.TypeDefinition || r.Parent.Kind == HandleKind.TypeReference || r.Parent.Kind == HandleKind.TypeSpecification
            ? TypeHandleName(m, (EntityHandle)r.Parent, named)
            : "?";
        return owner + "::" + md.GetString(r.Name);
    }

    string FieldData(Module m, FieldDefinitionHandle h)
    {
        var f = m.Md.GetFieldDefinition(h);
        if ((f.Attributes & FieldAttributes.HasFieldRVA) == 0) return "-";
        var data = m.Pe.GetSectionData(f.GetRelativeVirtualAddress());
        return Sha(data.GetContent(0, Math.Min(data.Length, 1 << 16)).ToArray());
    }

    // ---- indexing ----

    void IndexTypes(Module m)
    {
        foreach (var h in m.Md.TypeDefinitions)
        {
            var name = TypeDefName(m.Md, h);
            if (name == "<Module>") continue;
            types[name] = new TypeRec { Name = name, Module = m, Handle = h };
        }
    }

    // Own type names written inside an attribute blob (typeof arguments are stored as assembly-qualified names).
    IEnumerable<string> NamesInBlob(byte[] blob)
    {
        var run = new StringBuilder();
        foreach (var b in blob.Append((byte)0))
        {
            if (b >= 0x20 && b < 0x7f) { run.Append((char)b); continue; }
            if (run.Length >= 3)
            {
                var first = run.ToString().Split(',')[0].Trim();
                if (types.ContainsKey(first)) yield return first;
            }
            run.Clear();
        }
    }

    string Attributes(Module m, CustomAttributeHandleCollection attrs, List<string> whole, List<string>? ctors)
    {
        var md = m.Md;
        var sb = new StringBuilder();
        foreach (var ah in attrs)
        {
            var a = md.GetCustomAttribute(ah);
            var key = MethodTokenKey(m, a.Constructor, whole, out var simple);
            var blob = md.GetBlobBytes(a.Value);
            sb.Append('@').Append(key).Append('=').Append(Sha(blob)).Append('\n');
            whole.AddRange(NamesInBlob(blob));
            ctors?.Add(key);
        }
        return sb.ToString();
    }

    void IndexMembers(Module m)
    {
        var md = m.Md;
        foreach (var t in types.Values.Where(t => t.Module == m).ToList())
        {
            var def = md.GetTypeDefinition(t.Handle);
            var shape = new StringBuilder();
            shape.Append(def.Attributes).Append('\n');
            if (!def.BaseType.IsNil)
            {
                t.Base = TypeHandleName(m, def.BaseType, t.Whole);
                shape.Append("base ").Append(TypeHandleName(m, def.BaseType, null)).Append('\n');
            }
            foreach (var ih in def.GetInterfaceImplementations())
            {
                var iface = md.GetInterfaceImplementation(ih);
                shape.Append("implements ").Append(TypeHandleName(m, iface.Interface, t.Whole)).Append('\n');
            }
            foreach (var gp in def.GetGenericParameters())
                shape.Append("generic ").Append(md.GetString(md.GetGenericParameter(gp).Name)).Append('\n');
            var layout = def.GetLayout();
            shape.Append("layout ").Append(layout.PackingSize).Append(',').Append(layout.Size).Append('\n');
            shape.Append(Attributes(m, def.GetCustomAttributes(), t.Whole, t.AttributeCtors));
            foreach (var ah in def.GetCustomAttributes())
            {
                MethodTokenKey(m, md.GetCustomAttribute(ah).Constructor, new List<string>(), out var simple);
                if (simple.EndsWith("CollectionAttribute::.ctor", StringComparison.Ordinal) && simple.StartsWith("Xunit.", StringComparison.Ordinal)) t.UsesCollection = true;
                if (simple.EndsWith("CollectionDefinitionAttribute::.ctor", StringComparison.Ordinal)) t.IsCollectionDefinition = true;
            }
            t.IsAbstract = (def.Attributes & TypeAttributes.Abstract) != 0;
            foreach (var fh in def.GetFields())
            {
                var f = md.GetFieldDefinition(fh);
                var named = new List<string>();
                var type = f.DecodeSignature(new SigNames(this, m, named), null);
                t.Members.AddRange(named);
                shape.Append("field ").Append(md.GetString(f.Name)).Append(':').Append(type).Append(' ').Append(f.Attributes);
                var c = f.GetDefaultValue();
                if (!c.IsNil) shape.Append(" = ").Append(Sha(md.GetBlobBytes(md.GetConstant(c).Value)));
                if ((f.Attributes & FieldAttributes.HasFieldRVA) != 0)
                {
                    var rva = f.GetRelativeVirtualAddress();
                    var data = m.Pe.GetSectionData(rva);
                    shape.Append(" rva ").Append(Sha(data.GetContent(0, Math.Min(data.Length, 1 << 20)).ToArray()));
                }
                shape.Append('\n').Append(Attributes(m, f.GetCustomAttributes(), t.Whole, t.AttributeCtors));
            }
            foreach (var ph in def.GetProperties())
            {
                var p = md.GetPropertyDefinition(ph);
                var named = new List<string>();
                var sig = p.DecodeSignature(new SigNames(this, m, named), null);
                t.Members.AddRange(named);
                shape.Append("property ").Append(md.GetString(p.Name)).Append(':').Append(sig.ReturnType).Append('\n');
                shape.Append(Attributes(m, p.GetCustomAttributes(), t.Whole, t.AttributeCtors));
            }
            foreach (var eh in def.GetEvents())
                shape.Append("event ").Append(md.GetString(md.GetEventDefinition(eh).Name)).Append('\n');
            foreach (var nh in def.GetNestedTypes()) t.Nested.Add(TypeDefName(md, nh));
            foreach (var mh in def.GetMethods())
            {
                var rec = IndexMethod(m, mh, t.Name);
                t.Methods.Add(rec.Key);
                foreach (var a in md.GetMethodDefinition(mh).GetCustomAttributes())
                {
                    MethodTokenKey(m, md.GetCustomAttribute(a).Constructor, new List<string>(), out var simple);
                    var attrType = simple.Split("::")[0];
                    if (attrType.EndsWith("FactAttribute", StringComparison.Ordinal) || attrType.EndsWith("TheoryAttribute", StringComparison.Ordinal))
                        t.IsTestClass = true;
                }
            }
            t.Shape = Sha(shape.ToString());
        }
    }

    MethodInfoRec IndexMethod(Module m, MethodDefinitionHandle mh, string type)
    {
        var md = m.Md;
        var def = md.GetMethodDefinition(mh);
        var name = md.GetString(def.Name);
        var rec = new MethodInfoRec
        {
            Key = MethodDefKey(m, mh),
            Type = type,
            Virtual = (def.Attributes & MethodAttributes.Virtual) != 0,
            Ctor = name == ".ctor",
            Cctor = name == ".cctor",
        };
        var text = new StringBuilder();
        text.Append(def.Attributes).Append(' ').Append(def.ImplAttributes).Append('\n');
        text.Append(Attributes(m, def.GetCustomAttributes(), rec.Whole, rec.Calls));
        foreach (var ph in def.GetParameters())
        {
            var p = md.GetParameter(ph);
            text.Append("param ").Append(md.GetString(p.Name)).Append(' ').Append(p.Attributes);
            var c = p.GetDefaultValue();
            if (!c.IsNil) text.Append(" = ").Append(Sha(md.GetBlobBytes(md.GetConstant(c).Value)));
            text.Append('\n').Append(Attributes(m, p.GetCustomAttributes(), rec.Whole, rec.Calls));
        }
        foreach (var gp in def.GetGenericParameters())
            foreach (var ch in md.GetGenericParameter(gp).GetConstraints())
                text.Append("constraint ").Append(TypeHandleName(m, md.GetGenericParameterConstraint(ch).Type, rec.Whole)).Append('\n');
        if (def.RelativeVirtualAddress != 0)
        {
            var body = m.Pe.GetMethodBody(def.RelativeVirtualAddress);
            text.Append("locals ").Append(body.LocalVariablesInitialized).Append(' ').Append(body.MaxStack).Append('\n');
            if (!body.LocalSignature.IsNil)
            {
                var sig = md.GetStandaloneSignature(body.LocalSignature);
                var created = new List<string>();
                foreach (var l in sig.DecodeLocalSignature(new SigNames(this, m, created), null)) text.Append(l).Append(';');
                rec.Created.AddRange(created);
                text.Append('\n');
            }
            foreach (var region in body.ExceptionRegions)
            {
                text.Append("try ").Append(region.Kind).Append(' ').Append(region.TryOffset).Append(',').Append(region.TryLength)
                    .Append(' ').Append(region.HandlerOffset).Append(',').Append(region.HandlerLength);
                if (!region.CatchType.IsNil) text.Append(' ').Append(TypeHandleName(m, region.CatchType, rec.Created));
                text.Append('\n');
            }
            ReadIl(m, body.GetILReader(), rec, text);
        }
        rec.Hash = Sha(rec.Key + "\n" + text);
        methods[rec.Key] = rec;
        return rec;
    }

    void ReadIl(Module m, BlobReader il, MethodInfoRec rec, StringBuilder text)
    {
        var md = m.Md;
        while (il.RemainingBytes > 0)
        {
            short value = il.ReadByte();
            if (value == 0xFE) value = (short)(0xFE00 | il.ReadByte());
            if (!OpCodesByValue.TryGetValue(value, out var op)) { text.Append("?op ").Append(value).Append('\n'); break; }
            text.Append(op.Name);
            switch (op.OperandType)
            {
                case OperandType.InlineNone: break;
                case OperandType.ShortInlineBrTarget: case OperandType.ShortInlineI: case OperandType.ShortInlineVar:
                    text.Append(' ').Append(il.ReadByte()); break;
                case OperandType.InlineVar: text.Append(' ').Append(il.ReadUInt16()); break;
                case OperandType.InlineI: case OperandType.InlineBrTarget: text.Append(' ').Append(il.ReadInt32()); break;
                case OperandType.ShortInlineR: text.Append(' ').Append(il.ReadSingle().ToString("R")); break;
                case OperandType.InlineI8: text.Append(' ').Append(il.ReadInt64()); break;
                case OperandType.InlineR: text.Append(' ').Append(il.ReadDouble().ToString("R")); break;
                case OperandType.InlineSwitch:
                    var n = il.ReadInt32();
                    for (int i = 0; i < n; i++) text.Append(' ').Append(il.ReadInt32());
                    break;
                case OperandType.InlineString:
                {
                    var s = md.GetUserString(MetadataTokens.UserStringHandle(il.ReadInt32() & 0xFFFFFF));
                    rec.Strings.Add(s);
                    text.Append(' ').Append(JsonSerializer.Serialize(s));
                    break;
                }
                case OperandType.InlineSig:
                    text.Append(" sig ").Append(Sha(md.GetBlobBytes(md.GetStandaloneSignature((StandaloneSignatureHandle)MetadataTokens.EntityHandle(il.ReadInt32())).Signature)));
                    break;
                case OperandType.InlineMethod:
                {
                    var h = MetadataTokens.EntityHandle(il.ReadInt32());
                    var key = MethodTokenKey(m, h, rec.Whole, out var simple);
                    text.Append(' ').Append(key);
                    rec.Calls.Add(key);
                    if (op == OpCodes.Newobj) rec.Created.Add(simple.Split("::")[0]);
                    if (op == OpCodes.Call || op == OpCodes.Newobj || op == OpCodes.Ldftn) rec.Touched.Add(simple.Split("::")[0]);
                    if (DynamicApis.Contains(simple)) rec.Dynamic = true;
                    if (ReflectApis.Contains(simple)) rec.Reflects = true;
                    if (ResourceApis.Contains(simple)) rec.Resources = true;
                    break;
                }
                case OperandType.InlineField:
                {
                    var fh = MetadataTokens.EntityHandle(il.ReadInt32());
                    var name = FieldTokenName(m, fh, rec.Whole, out var owner);
                    text.Append(' ').Append(name);
                    if (fh.Kind == HandleKind.FieldDefinition && owner.StartsWith("<PrivateImplementationDetails>", StringComparison.Ordinal))
                        text.Append(" data ").Append(FieldData(m, (FieldDefinitionHandle)fh));
                    rec.Touched.Add(owner);
                    break;
                }
                case OperandType.InlineType: case OperandType.InlineTok:
                {
                    var h = MetadataTokens.EntityHandle(il.ReadInt32());
                    string name;
                    if (h.Kind is HandleKind.MethodDefinition or HandleKind.MemberReference or HandleKind.MethodSpecification)
                    {
                        name = MethodTokenKey(m, h, rec.Whole, out var simple);
                        rec.Calls.Add(name);
                    }
                    else if (h.Kind == HandleKind.FieldDefinition)
                    {
                        name = FieldTokenName(m, h, rec.Whole, out var owner);
                        rec.Touched.Add(owner);
                    }
                    else
                    {
                        name = TypeHandleName(m, h, rec.Whole);
                        if (op == OpCodes.Ldtoken) rec.Whole.Add(name);
                        else rec.Created.Add(name);
                    }
                    text.Append(' ').Append(name);
                    break;
                }
                default:
                    text.Append(" ?operand");
                    break;
            }
            text.Append('\n');
        }
    }

    // ---- reachability ----

    sealed class Walk
    {
        public readonly HashSet<string> Methods = new(StringComparer.Ordinal);
        public readonly HashSet<string> Touched = new(StringComparer.Ordinal);
        public readonly HashSet<string> Created = new(StringComparer.Ordinal);
        public readonly HashSet<string> Whole = new(StringComparer.Ordinal);
        public readonly HashSet<string> Strings = new(StringComparer.Ordinal);
        public readonly Stack<Action> Work = new();
        public bool Dynamic, Reflects, Resources;
    }

    void ReachMethod(Walk w, string key)
    {
        if (!methods.TryGetValue(key, out var rec) || !w.Methods.Add(key)) return;
        w.Work.Push(() =>
        {
            Touch(w, rec.Type);
            foreach (var c in rec.Calls) ReachMethod(w, c);
            foreach (var t in rec.Touched) Touch(w, t);
            foreach (var t in rec.Created) Create(w, t);
            foreach (var t in rec.Whole) WholeType(w, t);
            foreach (var s in rec.Strings) w.Strings.Add(s);
            w.Dynamic |= rec.Dynamic;
            w.Reflects |= rec.Reflects;
            w.Resources |= rec.Resources;
        });
    }

    void Touch(Walk w, string type)
    {
        if (!types.TryGetValue(type, out var t) || !w.Touched.Add(type)) return;
        foreach (var k in t.Methods) if (methods[k].Cctor) ReachMethod(w, k);
        foreach (var c in t.AttributeCtors) ReachMethod(w, c);
        foreach (var x in t.Whole) WholeType(w, x);
        var declaring = type.LastIndexOf('+');
        if (declaring > 0) Touch(w, type[..declaring]);
        if (t.Base != null) Touch(w, t.Base);
    }

    void Create(Walk w, string type)
    {
        if (!types.TryGetValue(type, out var t)) return;
        Touch(w, type);
        if (!w.Created.Add(type)) return;
        foreach (var k in t.Methods) if (methods[k].Virtual) ReachMethod(w, k);
        if (t.Base != null) Create(w, t.Base);
    }

    void WholeType(Walk w, string type)
    {
        if (!types.TryGetValue(type, out var t)) return;
        Create(w, type);
        if (!w.Whole.Add(type)) return;
        foreach (var k in t.Methods) ReachMethod(w, k);
        foreach (var n in t.Nested) WholeType(w, n);
        foreach (var x in t.Members) WholeType(w, x);
        if (t.Base != null) WholeType(w, t.Base);
    }

    public object Units(string testsPath, HashSet<string> contentNames)
    {
        var testModule = modules.First(m => m.Path == testsPath);
        var collections = types.Values.Where(t => t.IsCollectionDefinition).Select(t => t.Name).ToList();
        var testClasses = types.Values
            .Where(t => t.Module == testModule && !t.IsAbstract && HasTests(t))
            .OrderBy(t => t.Name, StringComparer.Ordinal)
            .ToList();
        var typeIndex = types.Keys.OrderBy(k => k, StringComparer.Ordinal).ToList();
        var position = typeIndex.Select((k, i) => (k, i)).ToDictionary(p => p.k, p => p.i, StringComparer.Ordinal);
        var typeHash = typeIndex.Select(k =>
        {
            var t = types[k];
            return Sha(t.Shape + "\n" + string.Join("\n", t.Methods.Select(x => methods[x].Hash)));
        }).ToList();
        var units = new SortedDictionary<string, object>(StringComparer.Ordinal);
        foreach (var t in testClasses)
        {
            var w = new Walk();
            WholeType(w, t.Name);
            if (t.UsesCollection) foreach (var c in collections) WholeType(w, c);
            Drain(w);
            if (w.Reflects)
            {
                // Reflection over members can reach any member of a created type: count those types whole.
                int before;
                do
                {
                    before = w.Whole.Count;
                    foreach (var c in w.Created.ToList()) WholeType(w, c);
                    Drain(w);
                } while (w.Whole.Count != before);
            }
            var lines = new List<string>();
            lines.AddRange(w.Methods.Select(k => "m " + k + "=" + methods[k].Hash));
            // The compiler's <PrivateImplementationDetails> holds every constant array and string-switch table of
            // the assembly, named by content: each use names the field it reads, so its whole shape is not an input.
            lines.AddRange(w.Touched.Where(k => !k.StartsWith("<PrivateImplementationDetails>", StringComparison.Ordinal))
                .Select(k => "t " + k + "=" + types[k].Shape));
            lines.Sort(StringComparer.Ordinal);
            if (w.Dynamic) lines.Add("dynamic " + wholeProgram);
            if (w.Resources) lines.Add("resources " + resources);
            var files = contentNames.Where(n => w.Strings.Any(s => s.Contains(n, StringComparison.Ordinal))).OrderBy(n => n, StringComparer.Ordinal).ToList();
            units[t.Name] = new
            {
                key = Sha(string.Join("\n", lines)),
                methods = w.Methods.Count,
                types = w.Touched.Where(position.ContainsKey).Select(k => position[k]).OrderBy(i => i).ToList(),
                dynamic = w.Dynamic,
                reflects = w.Reflects,
                resources = w.Resources,
                files,
            };
        }
        return new
        {
            version = 1,
            assemblies = modules.Select(m => m.Name).ToList(),
            type_names = typeIndex,
            type_hashes = typeHash,
            units,
        };
    }

    bool HasTests(TypeRec t)
    {
        for (var cur = t; cur != null; cur = cur.Base != null && types.TryGetValue(cur.Base, out var b) ? b : null)
            if (cur.IsTestClass) return true;
        return false;
    }

    static void Drain(Walk w)
    {
        while (w.Work.Count > 0) w.Work.Pop()();
    }
}
