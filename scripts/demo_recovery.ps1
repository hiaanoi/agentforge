$ErrorActionPreference = 'Stop'
# demo_core starts and resumes each durable phase using separate CLI processes.
& (Join-Path $PSScriptRoot 'demo_core.ps1')
