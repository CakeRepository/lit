import unittest

from concurrent_merge import readers
from concurrent_merge.readers import powershell
from concurrent_merge.structure import outline_merge
from reader_conformance import ReaderConformance

MODULE = b'''#Requires -Version 7.0
using namespace System.IO
using module ./Helpers.psm1

[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Root,
    [switch]$Force
)

Set-StrictMode -Version Latest
$script:Cache = @{}

<#
.SYNOPSIS
    Reads a manifest.
#>
function Get-Manifest {
    param([string]$Path)
    $text = Get-Content -Raw -LiteralPath $Path
    if ($text -match '\\{unbalanced') { throw "bad `"manifest`": $($Path) {" }
    $text | ConvertFrom-Json
}

# Writes it back.
Function Set-Manifest($Path, $Value) {
    $json = @"
{ "not": "code }" }
"@
    $json | Set-Content -LiteralPath $Path
}

filter Select-Name { $_.Name }

class Widget : System.IDisposable {
    [string] $Name = 'widget'
    hidden [int] $Count
    static [string[]] $Known = @('a', 'b')

    Widget() { $this.Count = 0 }
    Widget([string]$name) {
        $this.Name = $name
    }

    [void] Dispose() { }

    [string] Describe([int]$depth) {
        return "$($this.Name) at $depth"
    }
}

enum Color { Red; Green = 2 }

Export-ModuleMember -Function Get-Manifest, Set-Manifest
'''

PESTER = b'''BeforeAll {
    . $PSScriptRoot/Get-Manifest.ps1
}

Describe "Get-Manifest" {
    BeforeEach { $path = "TestDrive:/m.json" }

    Context "when the file is valid" {
        It "reads the name" {
            '{"name":"x"}' | Set-Content $path
            (Get-Manifest $path).name | Should -Be 'x'
        }

        It "reads the version" -TestCases @(@{ v = 1 }) {
            $true | Should -BeTrue
        }
    }

    Context "when the file is missing" {
        It "throws" { { Get-Manifest "nope" } | Should -Throw }
    }
}

Describe 'Set-Manifest' {
    It "writes the file" {
        Set-Manifest "TestDrive:/o.json" @{ a = 1 }
        Test-Path "TestDrive:/o.json" | Should -BeTrue
    }
}
'''

TINY = b"function a { 1 }\nfunction b { 2 }\n"


def add(blob, text):
    return blob + text


class PowerShellReaderTests(ReaderConformance, unittest.TestCase):
    PATH = "tools/Manifest.psm1"
    SAMPLES = (
        MODULE, PESTER, TINY, MODULE.replace(b"\n", b"\r\n"),
        "Describe -Name ‘it’’s ready’ { It “handles 😀” { } }\n".encode(),
        b"Describe -TestName scenario -Cases @{ X=1 }\nfunction actual {}\n",
        b'function f { "nested $( "inner $( 1 )" )" }\n',
        b'class X { X() {}\n X([int]$n) {}\n [int] ${a b} = 1\n}\n',
    )
    KEYS = (
        (0, ["using namespace system.io", "using module ./helpers.psm1", "param", "function get-manifest",
             "function set-manifest", "function select-name", "class widget", "enum color"]),
        (1, ["test Get-Manifest", "test Set-Manifest"]),
        (2, ["function a", "function b"]),
    )
    BROKEN = (
        b"function a {\n  1\n",
        b"function a { 1 }\n}\n",
        b"function a { 'open string }\n",
        b'function a { "open $(1 + ( ) }\n',
        b"<# never closed\nfunction a { }\n",
        b"function a { @'\nunterminated\n}\n",
        b"function a { @'\nnot at line start '@\n}\n",
        b"function a { ( 1 }\n",
        b"function\n",
        b"class A {\n  garbage here\n}\n",
        b"class A { [int]$a = }\n",
        b"param\n",
        b"\xff\xfe\x00\x00",
        b"\xc3\x28 function a {}\n",
    )
    MERGES = (
        # Adjacent functions edited on both sides.
        (MODULE, MODULE.replace(b"ConvertFrom-Json", b"ConvertFrom-Json -Depth 5"),
         MODULE.replace(b"Set-Content -LiteralPath", b"Set-Content -Encoding utf8 -LiteralPath"),
         MODULE.replace(b"ConvertFrom-Json", b"ConvertFrom-Json -Depth 5").replace(b"Set-Content -LiteralPath", b"Set-Content -Encoding utf8 -LiteralPath")),
        # Both sides add a different function at the end: ours first.
        (TINY, TINY + b"function c { 3 }\n", TINY + b"function d { 4 }\n", TINY + b"function c { 3 }\nfunction d { 4 }\n"),
        # The same function changed on both sides.
        (TINY, TINY.replace(b"{ 1 }", b"{ 10 }"), TINY.replace(b"{ 1 }", b"{ 11 }"), None),
        # Two different methods of one class.
        (MODULE, MODULE.replace(b"return \"$($this.Name) at $depth\"", b"return 'x'"),
         MODULE.replace(b"[void] Dispose() { }", b"[void] Dispose() { $this.Count = -1 }"),
         MODULE.replace(b"return \"$($this.Name) at $depth\"", b"return 'x'").replace(b"[void] Dispose() { }", b"[void] Dispose() { $this.Count = -1 }")),
        # Both sides add a property to the class.
        (MODULE, MODULE.replace(b"    hidden [int] $Count\n", b"    hidden [int] $Count\n    [bool] $Ready\n"),
         MODULE.replace(b"    hidden [int] $Count\n", b"    hidden [int] $Count\n    [datetime] $Seen\n"),
         MODULE.replace(b"    hidden [int] $Count\n", b"    hidden [int] $Count\n    [bool] $Ready\n    [datetime] $Seen\n")),
        # Pester: one side edits an It, the other adds an It to the same Context.
        (PESTER, PESTER.replace(b"(Get-Manifest $path).name | Should -Be 'x'", b"(Get-Manifest $path).name | Should -BeExactly 'x'"),
         PESTER.replace(b"    Context \"when the file is missing\"", b"    Context \"when the file is valid\" {\n    }\n\n    Context \"when the file is missing\""),
         None),
        # Pester: both sides add a different It to the same Context.
        (PESTER, PESTER.replace(b"        It \"reads the version\"", b"        It \"reads the author\" { }\n\n        It \"reads the version\""),
         PESTER.replace(b"        }\n    }\n\n    Context \"when the file is missing\"", b"        }\n\n        It \"reads the tags\" { }\n    }\n\n    Context \"when the file is missing\""),
         PESTER.replace(b"        It \"reads the version\"", b"        It \"reads the author\" { }\n\n        It \"reads the version\"").replace(
             b"        }\n    }\n\n    Context \"when the file is missing\"", b"        }\n\n        It \"reads the tags\" { }\n    }\n\n    Context \"when the file is missing\"")),
        # Pester: both sides add a different Describe at the end.
        (PESTER, PESTER + b"\nDescribe \"New-A\" {\n    It \"a\" { }\n}\n", PESTER + b"\nDescribe \"New-B\" {\n    It \"b\" { }\n}\n",
         PESTER + b"\nDescribe \"New-A\" {\n    It \"a\" { }\n}\n\nDescribe \"New-B\" {\n    It \"b\" { }\n}\n"),
        # Pester: edits in different Its of different Describes.
        (PESTER, PESTER.replace(b"Should -Throw", b"Should -Throw -ErrorId x"), PESTER.replace(b"Test-Path", b"Test-Path -PathType Leaf"),
         PESTER.replace(b"Should -Throw", b"Should -Throw -ErrorId x").replace(b"Test-Path", b"Test-Path -PathType Leaf")),
        # Function names are case-insensitive.
        (TINY, TINY.replace(b"function b", b"FUNCTION B").replace(b"2", b"22"), TINY.replace(b"{ 1 }", b"{ 11 }"),
         TINY.replace(b"function b", b"FUNCTION B").replace(b"2", b"22").replace(b"{ 1 }", b"{ 11 }")),
    )

    def test_pester_it_blocks_nest_in_context(self):
        _, (_, _, _, nodes) = readers.outline(self.PATH, PESTER)
        describe = nodes[0]
        self.assertEqual([c[0] for c in describe[3]], ["test when the file is valid", "test when the file is missing"])
        self.assertEqual([c[0] for c in describe[3][0][3]], ["test reads the name", "test reads the version"])

    def test_class_members(self):
        _, (_, _, _, nodes) = readers.outline(self.PATH, MODULE)
        widget = [n for n in nodes if n[0] == "class widget"][0]
        self.assertEqual([c[0] for c in widget[3]], [
            "property $name", "property $count", "property $known",
            "constructor widget(0)", "constructor widget(1)", "method dispose", "method describe"])

    def test_braces_in_strings_comments_and_here_strings_do_not_count(self):
        blob = (b"# } not code\n"
                b"function a {\n  $x = '}' + \"}\" + \"$( '}' )\" # {\n  <# } #>\n"
                b"  $h = @'\n}\n'@\n  $d = @\"\n}\n\"@\n  $v = ${a b}\n}\nfunction b { }\n")
        keys = [n[0] for n in readers.outline(self.PATH, blob)[1][3]]
        self.assertEqual(keys, ["function a", "function b"])

    def test_backtick_escapes_and_doubled_quotes(self):
        blob = b"function a {\n  \"a `\" } `\" \"\"} \" ; 'it''s }'\n  Write-Host `\n  1\n}\nfunction b {}\n"
        self.assertIsNotNone(readers.outline(self.PATH, blob))

    def test_comments_before_a_unit_stay_in_the_gap(self):
        _, (_, _, _, nodes) = readers.outline(self.PATH, MODULE)
        function = [n for n in nodes if n[0] == "function get-manifest"][0]
        self.assertTrue(MODULE[function[1]:function[2]].startswith(b"function Get-Manifest"))
        self.assertIn(b".SYNOPSIS", MODULE[:function[1]])

    def test_extents_follow_the_contract(self):
        blob = b"function a { 1 }   # tail\nfunction b { 2 }; function c { 3 }\n  function d { 4 }\n"
        nodes = readers.outline(self.PATH, blob)[1][3]
        self.assertEqual([blob[s:e] for _, s, e, _ in nodes],
                         [b"function a { 1 }", b"function b { 2 }", b"function c { 3 }\n", b"  function d { 4 }\n"])

    def test_utf8_bom_and_non_ascii_offsets(self):
        blob = b"\xef\xbb\xbf" + "function café { '\U0001f600' }\nfunction b { }\n".encode()
        nodes = readers.outline(self.PATH, blob)[1][3]
        self.assertEqual(nodes[0][0], "function café")
        self.assertEqual(nodes[0][1], 3)
        self.assertEqual(blob[nodes[1][1]:nodes[1][2]], b"function b { }\n")

    def test_utf16_with_bom_is_read_directly(self):
        # readers.outline() itself refuses any blob with a NUL byte, so UTF-16 is only reachable through the reader.
        text = "function a { 'x' }\r\nfunction b { }\r\n"
        for enc, bom in (("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff")):
            blob = bom + text.encode(enc)
            nodes = powershell.outline(blob, ".ps1")
            self.assertEqual([n[0] for n in nodes], ["function a", "function b"])
            self.assertEqual(blob[nodes[1][1]:nodes[1][2]].decode(enc), "function b { }\r\n")
            self.assertEqual(nodes[0][1], 2)
        self.assertIsNone(powershell.outline(b"\xff\xfe" + text.encode("utf-16-le")[:-1], ".ps1"))

    def test_psd1_manifest_has_no_units_but_is_readable(self):
        self.assertEqual(powershell.outline(b"@{\n  RootModule = 'x.psm1'\n  FunctionsToExport = @('a')\n}\n", ".psd1"), [])

    def test_lowercase_names_make_keys_match(self):
        a = readers.outline(self.PATH, b"function Get-X { }\n")[1][3][0][0]
        b = readers.outline(self.PATH, b"FUNCTION get-x { }\n")[1][3][0][0]
        self.assertEqual(a, b)

    def test_merge_result_outlines_again(self):
        merged = outline_merge(self.PATH, [TINY, TINY + b"function c { 3 }\n", TINY + b"function d { 4 }\n"])
        self.assertEqual([n[0] for n in readers.outline(self.PATH, merged)[1][3]],
                         ["function a", "function b", "function c", "function d"])


if __name__ == "__main__":
    unittest.main()
