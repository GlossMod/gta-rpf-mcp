# This entry point is internal. Method names, DLL, tree paths, and RPF entries
# are allowlisted here as well as in Python. The client cannot call this bridge.
param([Parameter(Mandatory=$true)][string]$Workspace)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$VerbosePreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
$nativeOut = [Console]::Out
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$nativeOut = [Console]::Out
try {
    if ($PSVersionTable.PSEdition -ne 'Desktop' -or $PSVersionTable.PSVersion -lt [Version]'5.1') { throw 'Expected Windows PowerShell 5.1.' }
    $framework = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full')
    if ($null -eq $framework) { throw 'Expected .NET Framework 4.8.' }
    try { if ([int]$framework.GetValue('Release', 0) -lt 528040) { throw 'Expected .NET Framework 4.8.' } }
    finally { $framework.Dispose() }
    $root = [IO.Path]::GetFullPath($Workspace)
    if ([IO.Directory]::GetCurrentDirectory() -ne $root) { throw 'Invalid work directory.' }
    $line = [Console]::In.ReadLine()
    if ($null -eq $line -or $line.Length -gt 8192) { throw 'Invalid bridge request.' }
    $request = ConvertFrom-Json -InputObject $line -ErrorAction Stop
    $commonList = 'common/data/dlclist.xml'
    $commonConfig = 'common/data/gameconfig.xml'
    $update = 'archives/update/update.rpf'
    $gmm = 'archives/gmm/dlc.rpf'
    $archive = $request.archive
    $entry = $request.entry
    $payload = $null
    switch -CaseSensitive ($request.operation) {
        'read' {
            if (!(($archive -ceq $update -and $entry -cin @($commonList, $commonConfig)) -or
                  ($archive -cmatch '^archives/dlc[0-9]{1,4}/dlc\.rpf$' -and $entry -ceq 'setup2.xml') -or
                  ($archive -ceq $gmm -and $entry -cin @('setup2.xml', 'data/pedmodelinfo.meta')))) { throw 'Read denied.' }
            $method = 'Read'
            $payload = @{rpf = [IO.Path]::Combine($root, $archive.Replace('/', '\')); filename = $entry.Replace('/', '\')}
        }
        'files' {
            if (!($archive -ceq $gmm -or $archive -cmatch '^archives/dlc[0-9]{1,4}/dlc\.rpf$')) { throw 'Listing denied.' }
            $method = 'GetFiles'
            $payload = @{rpf = [IO.Path]::Combine($root, $archive.Replace('/', '\'))}
        }
        'write' {
            if ($archive -cne $update -or
                !(($entry -ceq $commonList -and $request.source -ceq 'edit/dlclist.xml') -or
                  ($entry -ceq $commonConfig -and $request.source -ceq 'edit/gameconfig.xml'))) { throw 'Write denied.' }
            $method = 'Write'
            $payload = @{rpf = [IO.Path]::Combine($root, $update.Replace('/', '\')); targetFile = $entry.Replace('/', '\');
                         inputFile = [IO.Path]::Combine($root, $request.source.Replace('/', '\'))}
        }
        'create_gmm' {
            $method = 'Create'
            $payload = @{inputFolder = [IO.Path]::Combine($root, 'template\gmm');
                         outputPath = [IO.Path]::Combine($root, 'archives\gmm'); rpfName = 'dlc'}
        }
        default { throw 'Unknown bridge operation.' }
    }
    # PowerShell's ETS adapts ExpandoObject differently than the C# dynamic
    # binder. Construct the exact CLR payload type via its IDictionary API.
    $dyn = New-Object System.Dynamic.ExpandoObject
    $dictionary = [System.Collections.Generic.IDictionary[string,object]]
    $setter = $dictionary.GetMethod('set_Item')
    foreach ($item in $payload.GetEnumerator()) {
        $null = $setter.Invoke($dyn, [object[]]@([string]$item.Key, [string]$item.Value))
    }
    # Vendor Console text is not part of the internal JSON framing.
    [Console]::SetOut([IO.TextWriter]::Null)
    $assembly = [Reflection.Assembly]::LoadFrom([IO.Path]::Combine($root, 'release\rpf.dll'))
    $type = $assembly.GetType('Rpf.Program', $true, $false)
    $binding = [Reflection.BindingFlags]'Public, Instance, DeclaredOnly'
    $members = @($type.GetMethods($binding) | Where-Object {
        $_.Name -ceq $method -and -not $_.IsGenericMethod -and $_.ReturnType -eq [System.Threading.Tasks.Task[object]] -and
            $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType -eq [object]
    })
    if ($members.Count -ne 1) { throw 'Unexpected 3DM assembly signature.' }
    $instance = [Activator]::CreateInstance($type, $true)
    $task = $members[0].Invoke($instance, (,[object]$dyn))
    $answer = $task.GetAwaiter().GetResult()
    if ($answer -is [Exception]) { throw 'Vendor tool reported an exception.' }
    if ($method -eq 'Read' -and ($answer -isnot [string] -or $answer.Length -gt 8000000)) { throw 'Invalid RPF text.' }
    if ($method -eq 'GetFiles' -and ($answer -isnot [System.Collections.Generic.List[string]] -or $answer.Count -gt 100000)) { throw 'Invalid RPF inventory.' }
    if ($method -eq 'Write' -and $answer -cne '00') { throw 'The RPF write did not succeed.' }
    if ($method -eq 'Create' -and $null -ne $answer) { throw 'Unexpected Create result.' }
    $nativeOut.Write((ConvertTo-Json -Compress -Depth 3 -InputObject @{value = $answer}))
    $nativeOut.Flush()
    exit 0
} catch {
    # Do not send exception text/absolute paths or third-party output to MCP.
    [Console]::Error.WriteLine('RPF bridge rejected the operation.')
    exit 1
}
