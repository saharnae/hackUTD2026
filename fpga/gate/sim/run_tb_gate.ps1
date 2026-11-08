# run_tb_gate.ps1 - regenerate the vector file, compile and run tb_gate in Questa (batch).
# Exit code: 0 only if the transcript says "TB PASS"; 1 on any failure (compile, license, test).
#
#   powershell -ExecutionPolicy Bypass -File fpga\gate\sim\run_tb_gate.ps1
#
# Needs vlog/vsim on PATH (Questa-Intel FPGA Starter, C:\altera_lite\25.1std\questa_fse\win64)
# and a valid license (SALT_LICENSE_SERVER). Python 3 for the CSV converter.
$ErrorActionPreference = 'Continue'   # native tools write to stderr; judge by exit codes and the log
$sim = $PSScriptRoot
Push-Location $sim
try {
    python gate_csv2mem.py
    if ($LASTEXITCODE -ne 0) { Write-Host 'TB FAIL: gate_csv2mem.py failed'; exit 1 }

    if (-not (Test-Path work)) { vlib work | Out-Null }
    vlog -sv -lint +incdir+. ..\gate.sv tb_gate.sv
    if ($LASTEXITCODE -ne 0) { Write-Host 'TB FAIL: vlog failed'; exit 1 }

    if (Test-Path tb_gate.log) { Remove-Item tb_gate.log }
    vsim -c -voptargs=+acc -l tb_gate.log tb_gate +MEM=gate_vectors.mem -do 'run -all; quit -f'
    if (-not (Test-Path tb_gate.log)) { Write-Host 'TB FAIL: vsim wrote no log'; exit 1 }
    $log = Get-Content tb_gate.log -Raw
    if ($log -match '(?m)^#?\s*TB PASS' -and $log -notmatch '(?m)^#?\s*TB FAIL') { exit 0 }
    Write-Host 'TB FAIL (see fpga\gate\sim\tb_gate.log)'
    exit 1
} finally {
    Pop-Location
}
