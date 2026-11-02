# Service WMI persistant pour AORUS Open Control Center.
# Protocole : une requête JSON par ligne sur stdin -> une réponse JSON par ligne sur stdout.
# Les interfaces (GB_WMIACPI_Set / GB_WMIACPI_Get / RW_GMWMI) et les formats d'octets
# viennent de l'analyse de DeepFan.dll, Aorus15Keyboard.dll et SmartManager.dll.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$script:cache = @{}

function Reply($id, $ok, $data, $err) {
    $o = [ordered]@{ id = $id; ok = $ok }
    if ($ok) { $o.data = $data } else { $o.err = "$err" }
    [Console]::Out.WriteLine(($o | ConvertTo-Json -Compress -Depth 6))
    [Console]::Out.Flush()
}

function Get-Objs($cls) {
    # Comme le logiciel d'origine : TOUTES les instances (la bonne n'est pas forcément la première)
    if (-not $script:cache.ContainsKey($cls)) {
        $l = @(Get-WmiObject -Namespace 'root\WMI' -Class $cls -EnableAllPrivileges -Impersonation 3 -ErrorAction Stop)
        if ($l.Count -eq 0) { throw "Classe WMI '$cls' absente (pilote ACPI-WMI Gigabyte manquant ?)" }
        $script:cache[$cls] = $l
    }
    return $script:cache[$cls]
}
function Get-Obj($cls) { return (Get-Objs $cls)[0] }

function Cast($type, $v) {
    switch -Regex ("$type") {
        'UInt8'   { return [byte]$v }
        'UInt16'  { return [uint16]$v }
        'UInt32'  { return [uint32]$v }
        'UInt64'  { return [uint64]$v }
        'SInt8'   { return [sbyte]$v }
        'SInt16'  { return [int16]$v }
        'SInt32'  { return [int32]$v }
        'SInt64'  { return [int64]$v }
        'Boolean' { return [bool]$v }
        default   { return $v }
    }
}

function Invoke-GB($cls, $method, $margs) {
    $errs = @(); $res = $null; $done = $false
    foreach ($o in (Get-Objs $cls)) {
        foreach ($src in 'inst', 'class') {
            try {
                if ($src -eq 'inst') { $in = $o.GetMethodParameters($method) }
                else { $in = ([wmiclass]"\\.\root\WMI:$cls").GetMethodParameters($method) }
                if ($margs) {
                    foreach ($p in $margs.PSObject.Properties) {
                        $prop = $in.Properties[$p.Name]
                        if ($null -eq $prop) { throw "Parametre '$($p.Name)' inconnu pour $method (attendus : $(($in.Properties | % { $_.Name + ':' + $_.Type }) -join ', '))" }
                        $in[$p.Name] = Cast $prop.Type $p.Value
                    }
                }
                $out = $o.InvokeMethod($method, $in, $null)
                $r = [ordered]@{}
                if ($out) { foreach ($p in $out.Properties) { $r[$p.Name] = $p.Value } }
                if (-not $done) { $res = $r; $done = $true }
                break
            } catch { $errs += "$($o.__PATH) [$src] : $($_.Exception.Message)" }
        }
    }
    if (-not $done) { throw ($errs -join ' | ') }
    return $res
}

function Put-Kb([byte[]]$bytes) {
    # 3 méthodes, de la plus proche du logiciel d'origine (System.Management direct) à la plus tolérante
    $errs = @()
    try {
        Add-Type -AssemblyName System.Management -ErrorAction SilentlyContinue
        $co = New-Object System.Management.ConnectionOptions
        $co.EnablePrivileges = $true; $co.Impersonation = [System.Management.ImpersonationLevel]::Impersonate
        $sc = New-Object System.Management.ManagementScope('\\.\root\WMI', $co)
        $se = New-Object System.Management.ManagementObjectSearcher($sc, (New-Object System.Management.SelectQuery('RW_GMWMI')))
        $n = 0
        foreach ($mo in $se.Get()) { $mo.SetPropertyValue('BufferBytes', $bytes); [void]$mo.Put(); $n++ }
        if ($n -gt 0) { return }
        $errs += 'M1: aucune instance RW_GMWMI'
    } catch { $errs += "M1: $($_.Exception.Message)" }
    try {
        $n = 0
        foreach ($o in (Get-Objs 'RW_GMWMI')) { $o.BufferBytes = $bytes; [void]$o.Put(); $n++ }
        if ($n -gt 0) { return }
    } catch { $errs += "M2: $($_.Exception.Message)" }
    try {
        $n = 0
        foreach ($ci in (Get-CimInstance -Namespace root/WMI -ClassName RW_GMWMI -ErrorAction Stop)) {
            Set-CimInstance -InputObject $ci -Property @{ BufferBytes = $bytes } -ErrorAction Stop; $n++ }
        if ($n -gt 0) { return }
    } catch { $errs += "M3: $($_.Exception.Message)" }
    $script:cache = @{}
    throw ($errs -join ' | ')
}


function Get-Sensors {
    $res = [ordered]@{ lhm = @(); acpi = @() }
    try {
        $res.lhm = @(Get-CimInstance -Namespace 'root/LibreHardwareMonitor' -ClassName Sensor -ErrorAction Stop |
            Where-Object { $_.SensorType -in 'Temperature', 'Fan' -and $null -ne $_.Value } |
            ForEach-Object { [ordered]@{ n = $_.Name; t = $_.SensorType; v = [double]$_.Value; id = $_.Identifier } })
    } catch { }
    if ($res.lhm.Count -eq 0) {
        try {
            $res.acpi = @(Get-CimInstance -Namespace 'root/wmi' -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop |
                ForEach-Object { [math]::Round($_.CurrentTemperature / 10 - 273.15, 1) })
        } catch { }
    }
    return $res
}

function Get-Info {
    $i = [ordered]@{ ps = "$($PSVersionTable.PSVersion)"; classes = [ordered]@{}; methods = @() }
    try { $i.model = (Get-CimInstance Win32_ComputerSystem).Model } catch { }
    foreach ($c in 'GB_WMIACPI_Set', 'GB_WMIACPI_Get', 'RW_GMWMI') {
        try { [void](Get-Obj $c); $i.classes[$c] = $true } catch { $i.classes[$c] = "$($_.Exception.Message)" }
    }
    try {
        $i.methods = @((Get-WmiObject -Namespace 'root\WMI' -List -Class GB_WMIACPI_Set).Methods | ForEach-Object {
            $n = $_.Name; $p = ''
            try { $p = ($_.InParameters.Properties | ForEach-Object { $_.Name + ':' + $_.Type }) -join ',' } catch { }
            "$n($p)" })
        $i.instances = @((Get-Objs 'GB_WMIACPI_Set') | ForEach-Object { $_.__PATH })
        $i.getmethods = @((Get-WmiObject -Namespace 'root\WMI' -List -Class GB_WMIACPI_Get).Methods | ForEach-Object {
            $n = $_.Name; $p = ''
            try { $p = ($_.InParameters.Properties | ForEach-Object { $_.Name + ':' + $_.Type }) -join ',' } catch { }
            "$n($p)" })
    } catch { }
    return $i
}

[Console]::Out.WriteLine('{"ready":true}')
[Console]::Out.Flush()

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    if (-not $line.Trim()) { continue }
    $id = 0
    try {
        $req = $line | ConvertFrom-Json
        $id = $req.id
        switch ($req.op) {
            'ping'    { Reply $id $true 'pong' $null }
            'info'    { Reply $id $true (Get-Info) $null }
            'sensors' { Reply $id $true (Get-Sensors) $null }
            'call'    { Reply $id $true (Invoke-GB $req.cls $req.method $req.args) $null }
            'seq' {
                $log = @()
                foreach ($c in $req.calls) {
                    try {
                        [void](Invoke-GB $c.cls $c.method $c.args)
                        $log += [ordered]@{ m = $c.method; ok = $true }
                    } catch {
                        $log += [ordered]@{ m = $c.method; ok = $false; err = "$($_.Exception.Message)" }
                        if (-not $c.optional) { break }
                    }
                    if ($req.delay) { Start-Sleep -Milliseconds ([int]$req.delay) }
                }
                Reply $id $true $log $null
            }
            'kbput' { Put-Kb ([byte[]]@($req.bytes)); Reply $id $true 'ok' $null }
            'kbget' {
                $o = Get-WmiObject -Namespace 'root\WMI' -Class RW_GMWMI -EnableAllPrivileges | Select-Object -First 1
                Reply $id $true @($o.BufferBytes) $null
            }
            'quit'    { Reply $id $true 'bye' $null; exit 0 }
            default   { Reply $id $false $null "op inconnue : $($req.op)" }
        }
    } catch {
        $script:cache = @{}   # force une nouvelle résolution de l'objet WMI au prochain appel
        Reply $id $false $null $_.Exception.Message
    }
}
