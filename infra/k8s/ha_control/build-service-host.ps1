param([string]$Source,[string]$SourceSha256,[string]$ReferenceRoot,[string]$Pins,[string]$PinsSha256,[string]$Output)
# Offline metadata compilation only. No produced executable is run.
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
function Get-BuildHash([byte[]]$Bytes){
    $h=[Security.Cryptography.SHA256]::Create()
    try{return [BitConverter]::ToString($h.ComputeHash($Bytes)).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}
}
function Read-BuildPinned([string]$Path,[string]$Expected){
    if($Expected -cnotmatch '^[0-9a-f]{64}$'){throw 'invalid_build_pin'}
    $item=Get-Item -LiteralPath $Path -Force
    if($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.Length -gt 67108864){throw 'invalid_build_input'}
    # Hash, decode and parse exactly this buffer; never reopen source text.
    $bytes=[IO.File]::ReadAllBytes($item.FullName)
    if((Get-BuildHash $bytes) -cne $Expected){throw 'build_input_changed'}
    return ,$bytes
}
if(Test-Path -LiteralPath $Output){throw 'build_output_exists'}
$pinBytes=Read-BuildPinned $Pins $PinsSha256
$pinData=([Text.UTF8Encoding]::new($false,$true)).GetString($pinBytes)|ConvertFrom-Json
if(($pinData.PSObject.Properties.Name|Sort-Object) -join ',' -cne 'compiler,references'){throw 'invalid_build_pins'}
$compiler=@([Microsoft.CodeAnalysis.Compilation].Assembly,[Microsoft.CodeAnalysis.CSharp.CSharpCompilation].Assembly)
if(@($pinData.compiler.PSObject.Properties).Count -ne 2){throw 'invalid_compiler_pins'}
foreach($assembly in $compiler){
    $name=[IO.Path]::GetFileName($assembly.Location)
    $null=Read-BuildPinned $assembly.Location $pinData.compiler.$name
}
$refNames=@('mscorlib.dll','System.dll','System.Core.dll','System.ServiceProcess.dll')
if((($pinData.references.PSObject.Properties.Name|Sort-Object) -join ',') -cne (($refNames|Sort-Object) -join ',')){throw 'invalid_reference_pins'}
$refs=[Collections.Generic.List[Microsoft.CodeAnalysis.MetadataReference]]::new()
foreach($name in $refNames){
    $bytes=Read-BuildPinned (Join-Path $ReferenceRoot $name) $pinData.references.$name
    $refs.Add([Microsoft.CodeAnalysis.MetadataReference]::CreateFromImage([Collections.Immutable.ImmutableArray]::Create[byte]($bytes)))
}
$sourceBytes=Read-BuildPinned $Source $SourceSha256
$text=([Text.UTF8Encoding]::new($false,$true)).GetString($sourceBytes)
$tree=[Microsoft.CodeAnalysis.CSharp.CSharpSyntaxTree]::ParseText($text,[Microsoft.CodeAnalysis.CSharp.CSharpParseOptions]::Default,'windows-service-host.cs',[Text.Encoding]::UTF8)
$options=[Microsoft.CodeAnalysis.CSharp.CSharpCompilationOptions]::new([Microsoft.CodeAnalysis.OutputKind]::ConsoleApplication).WithPlatform([Microsoft.CodeAnalysis.Platform]::AnyCpu).WithOptimizationLevel([Microsoft.CodeAnalysis.OptimizationLevel]::Release).WithDeterministic($true)
$artifacts=[Collections.Generic.List[byte[]]]::new()
for($i=0;$i -lt 2;$i++){
    $compilation=[Microsoft.CodeAnalysis.CSharp.CSharpCompilation]::Create('EnforcedServiceV1',[Microsoft.CodeAnalysis.SyntaxTree[]]@($tree),$refs,$options)
    $stream=[IO.MemoryStream]::new()
    try{
        $result=$compilation.Emit($stream,$null,$null,$null,$null,$null,[Threading.CancellationToken]::None)
        if(-not $result.Success -or $stream.Length -lt 512){throw ('host_compile_failed: '+(($result.Diagnostics|ForEach-Object {$_.ToString()}) -join ';'))}
        $artifacts.Add($stream.ToArray())
    }finally{$stream.Dispose()}
}
$hash=Get-BuildHash $artifacts[0]
if($hash -cne (Get-BuildHash $artifacts[1])){throw 'nondeterministic_host_build'}
# CreateNew protects old artifacts even when a caller supplied an existing path.
$null=[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Output)))
$file=[IO.File]::Open($Output,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
try{$file.Write($artifacts[0],0,$artifacts[0].Length);$file.Flush()}finally{$file.Dispose()}
[ordered]@{source_sha256=$SourceSha256;artifact_sha256=$hash;pins_sha256=$PinsSha256;compiler=$pinData.compiler;references=$pinData.references;target='.NETFramework,Version=v4.8';platform='AnyCPU';output_kind='ConsoleApplication';deterministic_outputs=2;native_accepted=$false}|ConvertTo-Json -Depth 5 -Compress
