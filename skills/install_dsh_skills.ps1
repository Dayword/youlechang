<#
.SYNOPSIS
  把 skills/manifest.json 列出的 Skill 安装到 DeepSeek Harness (DSH) 用户技能目录。
  支持布局：root（整仓一个技能）/ path（仓库内 skills/<name>）/ auto（根有 SKILL.md 用单技能，
  否则自动安装仓库顶层每个含 SKILL.md 的子目录为独立技能，例如 deploysquad-ai/recon 的 micro/western 等）。
  内容来源（按优先级）：
    A. 本地 <VendorRoot>/<name>（你已下载解压的内容）
    B. 直连 GitHub：git clone --depth 1 <repo> 到临时目录（需本机可访问 github.com）
  安装目标：$DSH_HOME/skills 或 ~/.dsh/skills
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\skills\install_dsh_skills.ps1
#>
param(
  [string]$ManifestPath = "skills/manifest.json",
  [string]$SkillsDir = "",
  [string]$VendorRoot = "skills/_src",
  [switch]$Force
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
$manifestPath = Join-Path $root $ManifestPath
if (-not (Test-Path $manifestPath)) { throw "找不到清单: $manifestPath" }
$manifest = Get-Content $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json

if (-not $SkillsDir) {
  $homeBase = if ($env:DSH_HOME) { $env:DSH_HOME } else { Join-Path $HOME '.dsh' }
  $SkillsDir = Join-Path $homeBase 'skills'
}
New-Item -ItemType Directory -Force -Path $SkillsDir | Out-Null
Write-Host "== DSH 用户技能目录: $SkillsDir"
$vendorAbs = Join-Path $root $VendorRoot
New-Item -ItemType Directory -Force -Path $vendorAbs | Out-Null

function Install-SingleSkill {
  param([string]$SrcFolder, [string]$Name, [switch]$Overwrite)
  if (-not (Test-Path (Join-Path $SrcFolder 'SKILL.md'))) {
    $md = Get-ChildItem $SrcFolder -Filter *.md -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $md) { Write-Warning "跳过 $Name：找不到 SKILL.md / .md"; return $false }
  }
  $dest = Join-Path $SkillsDir $Name
  if (Test-Path $dest) {
    if (-not $Overwrite) { Write-Warning "已存在同名技能: $Name（跳过；加 -Force 覆盖）"; return $false }
    Remove-Item $dest -Recurse -Force
  }
  New-Item -ItemType Directory -Force -Path $dest | Out-Null
  Copy-Item (Join-Path $SrcFolder '*') -Destination $dest -Recurse -Force
  $skillFile = Join-Path $dest 'SKILL.md'
  if (-not (Test-Path $skillFile)) {
    $md = Get-ChildItem $dest -Filter *.md | Select-Object -First 1
    if ($md) { Move-Item $md.FullName $skillFile -Force }
  }
  Write-Host "✔ 已安装: $Name -> $dest"
  return $true
}

function Install-ByLayout {
  param([string]$RepoDir, [hashtable]$E)
  $name = $E.name
  $layout = $E.layout
  $src = $RepoDir
  if ($layout -eq 'path') {
    $src = Join-Path $RepoDir $E.subPath
    if (-not (Test-Path $src)) { Write-Warning "找不到子路径 $($E.subPath)（$name）——请核对 manifest"; return }
    [void](Install-SingleSkill -SrcFolder $src -Name $name -Overwrite:$Force)
  } elseif ($layout -eq 'auto') {
    # 根目录直接是单技能
    if (Test-Path (Join-Path $RepoDir 'SKILL.md') -or (Get-ChildItem $RepoDir -Filter *.md | Where-Object { $_.BaseName -eq $RepoDir })) {
      [void](Install-SingleSkill -SrcFolder $RepoDir -Name $name -Overwrite:$Force)
    } else {
      $children = Get-ChildItem $RepoDir -Directory | Where-Object {
        Test-Path (Join-Path $_.FullName 'SKILL.md') -or (Get-ChildItem $_.FullName -Filter *.md -ErrorAction SilentlyContinue)
      }
      if (-not $children) {
        # 兜底：看二级 skills/ 目录
        $l2 = Get-ChildItem (Join-Path $RepoDir 'skills') -Directory -ErrorAction SilentlyContinue | Where-Object {
          Test-Path (Join-Path $_.FullName 'SKILL.md') -or (Get-ChildItem $_.FullName -Filter *.md -ErrorAction SilentlyContinue)
        }
        if ($l2) { $children = $l2 }
      }
      if (-not $children) {
        Write-Warning "$name：仓库未发现任何含 SKILL.md 的技能目录，请人工查看后手动安装"; return
      }
      foreach ($c in $children) { [void](Install-SingleSkill -SrcFolder $c.FullName -Name $c.Name -Overwrite:$Force) }
    }
  } else { # root
    [void](Install-SingleSkill -SrcFolder $RepoDir -Name $name -Overwrite:$Force)
  }
}

$done = @(); $skipped = @()
foreach ($e in $manifest.entries) {
  if ($e.status -ne 'ready') { Write-Host "⏳ 跳过（非 ready）：$($e.zh)"; continue }
  $name = $e.name
  $local = Join-Path $vendorAbs $name
  $repoDir = $null
  if (Test-Path $local) {
    $repoDir = $local
    Write-Host "使用本地源码: $local"
  } elseif ($e.source -eq 'github' -and $e.repo) {
    $tmp = Join-Path $vendorAbs "_git_$name"
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
    Write-Host "git clone $($e.repo) ..."
    git clone --depth 1 --branch $e.branch $e.repo $tmp 2>$null
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $tmp)) {
      Write-Warning "git 拉取失败（本机需能访问 github.com）：$($e.repo)。可手动下载 zip 解压到 $vendorAbs\$name 后重试。"
      $skipped += $name; continue
    }
    $repoDir = $tmp
  } else {
    Write-Warning "$name：无本地源码且无仓库地址"; $skipped += $name; continue
  }
  Install-ByLayout -RepoDir $repoDir -E @{ name = $name; layout = $e.layout; subPath = $e.subPath }
  if ($repoDir -like '*_git_*') { Remove-Item $repoDir -Recurse -Force -ErrorAction SilentlyContinue }
  $done += $name
}

Write-Host ''
Write-Host "== 汇总 =="
Write-Host "已处理: $($done -join ', ')"
if ($skipped.Count) { Write-Host "失败/跳过: $($skipped -join ', ')" }
Write-Host ''
Write-Host "说明：DSH 技能提供方自动监听 $SkillsDir。新技能下一轮会话目录可见；命名需 kebab-case，目录含带 name+description frontmatter 的 SKILL.md。"
