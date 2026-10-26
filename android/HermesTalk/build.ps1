# Build the Hermes Talk Android shell with the repo-local portable toolchain.
$ErrorActionPreference = 'Stop'

$here = $PSScriptRoot
$repo = (Resolve-Path (Join-Path $here '..\..')).Path
$sdk = Join-Path $repo '.tools\android-sdk'
$jdk = Join-Path $repo '.tools\jdk21'
$buildTools = Join-Path $sdk 'build-tools\37.0.0'
$androidJar = Join-Path $sdk 'platforms\android-37\android.jar'
$out = Join-Path $repo '.tools\android-build\hermes-talk'
$unsigned = Join-Path $out 'unsigned.apk'
$aligned = Join-Path $out 'aligned.apk'
$signed = Join-Path $out 'hermes-talk.apk'
$keystore = Join-Path $env:USERPROFILE '.android\debug.keystore'

foreach ($p in @($buildTools, $androidJar, $keystore)) {
    if (-not (Test-Path $p)) { throw "missing prerequisite: $p" }
}

New-Item -ItemType Directory -Force -Path $out, (Join-Path $out 'classes'), (Join-Path $out 'dex') | Out-Null
Remove-Item $unsigned, $aligned, $signed -Force -ErrorAction SilentlyContinue

$env:JAVA_HOME = $jdk
$env:PATH = "$(Join-Path $jdk 'bin');$buildTools;$env:PATH"

Write-Output '=== aapt2 link ==='
& (Join-Path $buildTools 'aapt2.exe') link `
    -o $unsigned `
    -I $androidJar `
    --manifest (Join-Path $here 'AndroidManifest.xml') `
    --min-sdk-version 32 `
    --target-sdk-version 37
if ($LASTEXITCODE -ne 0) { throw 'aapt2 link failed' }

Write-Output '=== javac ==='
$javaSources = @(Get-ChildItem (Join-Path $here 'src') -Recurse -Filter *.java | Select-Object -ExpandProperty FullName)
$javacArgs = @('--release', '8', '-encoding', 'UTF-8', '-classpath', $androidJar, '-d', (Join-Path $out 'classes')) + $javaSources
& (Join-Path $jdk 'bin\javac.exe') @javacArgs
if ($LASTEXITCODE -ne 0) { throw 'javac failed' }

Write-Output '=== d8 ==='
$classFiles = @(Get-ChildItem (Join-Path $out 'classes') -Recurse -Filter *.class | Select-Object -ExpandProperty FullName)
$d8Args = @('--lib', $androidJar, '--min-api', '32', '--output', (Join-Path $out 'dex')) + $classFiles
& (Join-Path $buildTools 'd8.bat') @d8Args
if ($LASTEXITCODE -ne 0) { throw 'd8 failed' }

Write-Output '=== add classes.dex to APK ==='
Add-Type -AssemblyName System.IO.Compression.FileSystem
$dex = Join-Path $out 'dex\classes.dex'
$zip = [System.IO.Compression.ZipFile]::Open($unsigned, 'Update')
try {
    $existing = $zip.GetEntry('classes.dex')
    if ($existing) { $existing.Delete() }
    [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
        $zip, $dex, 'classes.dex', [System.IO.Compression.CompressionLevel]::Optimal) | Out-Null
} finally {
    $zip.Dispose()
}

Write-Output '=== zipalign + sign ==='
& (Join-Path $buildTools 'zipalign.exe') -f -p 4 $unsigned $aligned
if ($LASTEXITCODE -ne 0) { throw 'zipalign failed' }
& (Join-Path $buildTools 'apksigner.bat') sign `
    --ks $keystore --ks-pass pass:android --key-pass pass:android --ks-key-alias androiddebugkey `
    --out $signed $aligned
if ($LASTEXITCODE -ne 0) { throw 'apksigner failed' }
& (Join-Path $buildTools 'apksigner.bat') verify --verbose $signed
if ($LASTEXITCODE -ne 0) { throw 'apksigner verify failed' }

Write-Output ''
Write-Output ("built: {0} ({1} bytes)" -f $signed, (Get-Item $signed).Length)
Write-Output ("sha256: {0}" -f (Get-FileHash $signed -Algorithm SHA256).Hash.ToLowerInvariant())
