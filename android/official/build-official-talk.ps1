# Patch the official Hermes Agent decompiled project with the Talk surface.
# Compiles android/official/TalkActivity.java -> dex -> smali, merges it into
# .tools/decompile/apktool-src, rebuilds with Apktool, and signs with the local
# debug keystore. Output: .tools/decompile/hermes-agent-talk.apk
$ErrorActionPreference = 'Stop'

$here = $PSScriptRoot
$repo = (Resolve-Path (Join-Path $here '..\..')).Path
$jdk = Join-Path $repo '.tools\jdk21'
$sdk = Join-Path $repo '.tools\android-sdk'
$buildTools = Join-Path $sdk 'build-tools\37.0.0'
$androidJar = Join-Path $sdk 'platforms\android-37\android.jar'
$baksmali = Join-Path $repo '.tools\downloads\baksmali-3.0.10-fat-release.jar'
$apktool = Join-Path $repo '.tools\downloads\apktool_2.11.1.jar'
$frame = Join-Path $repo '.tools\apktool-framework'
$project = Join-Path $repo '.tools\decompile\apktool-src'
$work = Join-Path $repo '.tools\decompile\official-patch'
$outApk = Join-Path $repo '.tools\decompile\hermes-agent-talk.apk'
$keystore = Join-Path $env:USERPROFILE '.android\debug.keystore'

foreach ($p in @($buildTools, $androidJar, $baksmali, $apktool, $project, $keystore)) {
    if (-not (Test-Path $p)) { throw "missing prerequisite: $p" }
}

$tmp = Join-Path $repo '.tools\temp'
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$env:TEMP = $tmp
$env:TMP = $tmp
$env:JAVA_HOME = $jdk
$env:PATH = "$(Join-Path $jdk 'bin');$buildTools;$env:PATH"

Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path (Join-Path $work 'classes'), (Join-Path $work 'dex'), (Join-Path $work 'smali') | Out-Null

Write-Output '=== javac ==='
& (Join-Path $jdk 'bin\javac.exe') --release 8 -encoding UTF-8 `
    -classpath $androidJar -d (Join-Path $work 'classes') `
    (Join-Path $here 'TalkActivity.java')
if ($LASTEXITCODE -ne 0) { throw 'javac failed' }

Write-Output '=== d8 ==='
$classFiles = Get-ChildItem (Join-Path $work 'classes') -Recurse -Filter *.class | Select-Object -ExpandProperty FullName
$d8Args = @('--lib', $androidJar, '--min-api', '32', '--output', (Join-Path $work 'dex')) + $classFiles
& (Join-Path $buildTools 'd8.bat') @d8Args
if ($LASTEXITCODE -ne 0) { throw 'd8 failed' }

Write-Output '=== baksmali ==='
& (Join-Path $jdk 'bin\java.exe') -jar $baksmali d (Join-Path $work 'dex\classes.dex') -o (Join-Path $work 'smali')
if ($LASTEXITCODE -ne 0) { throw 'baksmali failed' }

Write-Output '=== merge smali into official project ==='
$targetDir = Join-Path $project 'smali_classes2\ai\nousresearch\hermes\mobile'
New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
Get-ChildItem $targetDir -Filter 'TalkActivity*.smali' -ErrorAction SilentlyContinue | Remove-Item -Force
$smaliFiles = Get-ChildItem (Join-Path $work 'smali') -Recurse -Filter 'TalkActivity*.smali'
if (-not $smaliFiles) { throw 'no TalkActivity smali produced' }
foreach ($f in $smaliFiles) {
    Copy-Item $f.FullName (Join-Path $targetDir $f.Name) -Force
    Write-Output ("  + " + $f.Name)
}

Write-Output '=== apktool build ==='
if (Test-Path $outApk) { Remove-Item $outApk -Force }
& (Join-Path $jdk 'bin\java.exe') -jar $apktool b -p $frame -o $outApk $project
if ($LASTEXITCODE -ne 0) { throw 'apktool build failed' }

Write-Output '=== merge arm64 native libraries from the Play split ==='
$splitApk = Join-Path $repo '.tools\apk\split_config.arm64_v8a.apk'
if (Test-Path $splitApk) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::Open($outApk, 'Update')
    $splitZip = [System.IO.Compression.ZipFile]::OpenRead($splitApk)
    try {
        foreach ($entry in $splitZip.Entries) {
            if ($entry.FullName -notlike 'lib/arm64-v8a/*.so') { continue }
            $existing = $zip.GetEntry($entry.FullName)
            if ($existing) { $existing.Delete() }
            $new = $zip.CreateEntry($entry.FullName, [System.IO.Compression.CompressionLevel]::NoCompression)
            $src = $entry.Open()
            $dst = $new.Open()
            try { $src.CopyTo($dst) } finally { $dst.Dispose(); $src.Dispose() }
            Write-Output ("  + " + $entry.FullName)
        }
    } finally {
        $splitZip.Dispose()
        $zip.Dispose()
    }
}

Write-Output '=== zipalign + sign ==='
$aligned = Join-Path $work 'aligned.apk'
& (Join-Path $buildTools 'zipalign.exe') -f -p 4 $outApk $aligned
if ($LASTEXITCODE -ne 0) { throw 'zipalign failed' }
& (Join-Path $buildTools 'apksigner.bat') sign `
    --ks $keystore --ks-pass pass:android --key-pass pass:android --ks-key-alias androiddebugkey `
    --out $outApk $aligned
if ($LASTEXITCODE -ne 0) { throw 'apksigner failed' }
& (Join-Path $buildTools 'apksigner.bat') verify --verbose $outApk | Select-Object -First 12

Write-Output '=== verify TalkActivity in built APK ==='
& (Join-Path $buildTools 'aapt2.exe') dump badging $outApk 2>&1 |
    Select-String -Pattern 'Hermes Talk|TalkActivity' | Select-Object -First 6

Write-Output ''
Write-Output ("built: {0} ({1} bytes)" -f $outApk, (Get-Item $outApk).Length)
Write-Output ("sha256: {0}" -f (Get-FileHash $outApk -Algorithm SHA256).Hash.ToLowerInvariant())
