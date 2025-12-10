# convert-doc-to-docx.ps1
# Batch converts all .doc files in a folder to .docx format
# Requires Microsoft Word to be installed

param(
    [Parameter(Mandatory=$true)]
    [string]$InputFolder,
    
    [Parameter(Mandatory=$false)]
    [string]$OutputFolder = "",
    
    [Parameter(Mandatory=$false)]
    [switch]$Recurse
)

# If no output folder specified, save in same location as input
if ($OutputFolder -eq "") {
    $OutputFolder = $InputFolder
}

# Create output folder if it doesn't exist
if (!(Test-Path $OutputFolder)) {
    New-Item -ItemType Directory -Path $OutputFolder | Out-Null
    Write-Host "Created output folder: $OutputFolder"
}

# Get all .doc files
if ($Recurse) {
    $docFiles = Get-ChildItem -Path $InputFolder -Filter "*.doc" -Recurse | Where-Object { $_.Extension -eq ".doc" }
} else {
    $docFiles = Get-ChildItem -Path $InputFolder -Filter "*.doc" | Where-Object { $_.Extension -eq ".doc" }
}

if ($docFiles.Count -eq 0) {
    Write-Host "No .doc files found in $InputFolder" -ForegroundColor Yellow
    exit
}

Write-Host "Found $($docFiles.Count) .doc file(s) to convert" -ForegroundColor Cyan

# Start Word
try {
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0  # Suppress alerts
} catch {
    Write-Host "Error: Could not start Microsoft Word. Make sure Word is installed." -ForegroundColor Red
    exit 1
}

$converted = 0
$failed = 0

foreach ($docFile in $docFiles) {
    $inputPath = $docFile.FullName
    $outputPath = Join-Path $OutputFolder ($docFile.BaseName + ".docx")
    
    # Skip if .docx already exists
    if (Test-Path $outputPath) {
        Write-Host "  Skipping (already exists): $($docFile.Name)" -ForegroundColor Yellow
        continue
    }
    
    Write-Host "  Converting: $($docFile.Name)" -NoNewline
    
    try {
        $doc = $word.Documents.Open($inputPath)
        # 16 = wdFormatXMLDocument (.docx)
        $doc.SaveAs([ref]$outputPath, [ref]16)
        $doc.Close()
        Write-Host " -> $($docFile.BaseName).docx" -ForegroundColor Green
        $converted++
    } catch {
        Write-Host " FAILED: $_" -ForegroundColor Red
        $failed++
    }
}

# Cleanup
$word.Quit()
[System.Runtime.Interopservices.Marshal]::ReleaseComObject($word) | Out-Null

Write-Host ""
Write-Host "Conversion complete!" -ForegroundColor Cyan
Write-Host "  Converted: $converted"
if ($failed -gt 0) {
    Write-Host "  Failed: $failed" -ForegroundColor Red
}
