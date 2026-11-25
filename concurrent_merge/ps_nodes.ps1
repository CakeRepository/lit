$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$texts = ConvertFrom-Json ([Console]::In.ReadToEnd()) -NoEnumerate
$result = foreach ($text in $texts) {
    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tokens, [ref]$errors)
    $nodes = foreach ($node in $ast.FindAll({ $true }, $true)) {
        $type = $node.GetType().Name
        $name = switch ($type) {
            'FunctionDefinitionAst' {
                if ($node.Parent.GetType().Name -ne 'FunctionMemberAst') { $node.Name }
            }
            'TypeDefinitionAst' { $node.Name }
            'FunctionMemberAst' { $node.Name + '(' + (($node.Parameters | ForEach-Object { $_.StaticType.FullName }) -join ',') + ')' }
            'PropertyMemberAst' { $node.Name }
            'NamedBlockAst' { if (-not $node.Unnamed) { $node.BlockKind.ToString() } }
            'StatementBlockAst' {
                $parent = $node.Parent
                switch ($parent.GetType().Name) {
                    'IfStatementAst' {
                        foreach ($clause in $parent.Clauses) {
                            if ([object]::ReferenceEquals($node, $clause.Item2)) {
                                'if ' + $clause.Item1.Extent.Text
                            }
                        }
                        if ([object]::ReferenceEquals($node, $parent.ElseClause)) {
                            'else ' + (($parent.Clauses | ForEach-Object { $_.Item1.Extent.Text }) -join '|')
                        }
                    }
                    'TryStatementAst' {
                        if ([object]::ReferenceEquals($node, $parent.Body)) { 'try' } else { 'finally' }
                    }
                    'CatchClauseAst' { 'catch ' + (($parent.CatchTypes | ForEach-Object { $_.Extent.Text }) -join ',') }
                    'WhileStatementAst' { 'while ' + $parent.Condition.Extent.Text }
                    'ForEachStatementAst' { 'foreach ' + $parent.Variable.Extent.Text + ' in ' + $parent.Condition.Extent.Text }
                }
            }
        }
        if ($name) {
            @{ key = "$type/$($name.ToLowerInvariant())"; start = $node.Extent.StartOffset; end = $node.Extent.EndOffset }
        }
    }
    @{ valid = ($errors.Count -eq 0); nodes = @($nodes) }
}
[Console]::Write((ConvertTo-Json -InputObject @($result) -Depth 5 -Compress))
