# Compile dist\aion2-pet-meter.exe (un seul fichier, sans console).
# Prérequis : python -m pip install -r requirements.txt
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
if (-not (Test-Path "$root\version.txt")) { Set-Content -Path "$root\version.txt" -Value "dev" -NoNewline }

# PIL._avif (7,5 Mo, le plus gros fichier de l'exe) n'est pas embarqué : le Pet Meter ne lit ni n'écrit d'AVIF, et l'exe
# « un seul fichier » se décompresse dans %TEMP% à chaque lancement (40 Mo, ~1 s mesurée sur la v0.21.3) : 18 % de moins à
# écrire. Pillow se passe d'un format absent (import manqué toléré).

python -m PyInstaller --noconfirm --onefile --windowed `
    --name aion2-pet-meter `
    --icon "$root\assets\icon.ico" `
    --add-data "$root\data\names.json;data" `
    --add-data "$root\data\npc_pets.json;data" `
    --add-data "$root\data\skills.json;data" `
    --add-data "$root\assets\icon.ico;assets" `
    --add-data "$root\version.txt;." `
    --collect-submodules scapy `
    --collect-all winocr `
    --collect-all winrt `
    --exclude-module PIL._avif `
    --exclude-module PIL.AvifImagePlugin `
    --distpath "$root\dist" `
    --workpath "$root\build" `
    --specpath "$root\build" `
    "$root\aion2_pet_meter.py"

if ($LASTEXITCODE -ne 0) { throw "PyInstaller a échoué" }
Write-Host "Compilé : $root\dist\aion2-pet-meter.exe"
