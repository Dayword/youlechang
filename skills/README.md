# 安装 DSH（DeepSeek Harness）自定义 Skills

目标：把 GitHub 上的几个 Agent Skill 装成当前 Agent 可直接用 `skill` 工具加载的技能。

## DSH 技能目录规范（已核实）
- 用户级技能根目录：`$DSH_HOME/skills`（本机 `C:\Users\86133\.dsh\skills`，不存在则自动创建）
- 单个技能 = `<技能名>/SKILL.md`；或扁平文件 `<技能名>.md`
- frontmatter 必需：`name`（kebab-case）、`description`；可选 `whenToUse`/`metadata` 等
- 提供方自动 watch 该目录，新技能在下一轮会话目录中可见

## 当前清单（skills/manifest.json，全部 ready）
| 安装技能 | 说明 | 来源 | 布局 |
|---|---|---|---|
| context-compression | 上下文压缩 | [Agent-Skills-for-Context-Engineering](https://github.com/muratcankoylan/Agent-Skills-for-Context-Engineering) `skills/context-compression` | path |
| context-optimization | 上下文优化 | 同上 `skills/context-optimization` | path |
| filesystem-context | 文件系统上下文 | 同上 `skills/filesystem-context` | path |
| recon（内含 micro / western 等） | 代码侦察与向量查找 | [deploysquad-ai/recon](https://github.com/deploysquad-ai/recon) | auto（自动发现顶层每个含 SKILL.md 的子目录并分别安装） |
| skill-optimizer | 技能优化器 | [huang-hf/skill-optimizer](https://github.com/huang-hf/skill-optimizer) | root |

> #6（muratcankoylan 整仓）与 1-3 同源，按你的确认忽略。

## 使用
### 方式 A：本机能访问 github.com（推荐）
```powershell
powershell -ExecutionPolicy Bypass -File .\skills\install_dsh_skills.ps1
```
脚本对每个条目 `git clone --depth 1`，再按布局安装到 `$HOME\.dsh\skills`；
recon 会自动把仓库顶层每个含 SKILL.md 的子目录（micro / western / …）分别安装。
（重跑会提示已存在，加 `-Force` 覆盖；只想改目标目录用 `-SkillsDir <路径>`。）

### 方式 B：把内容给我（本沙箱访问不了 GitHub）
从 GitHub 下载各仓库 zip，解压成：
```
skills/_src/context-compression/...
skills/_src/context-optimization/...
skills/_src/filesystem-context/...
skills/_src/recon/...            （整仓解压即可，auto 会自动挑子技能）
skills/_src/skill-optimizer/...
```
然后告诉我，我会执行安装到 `C:\Users\86133\.dsh\skills`（工作区外写入会弹一次授权）并验证目录/文件。

## 校验方法
安装后检查：`Get-ChildItem $HOME\.dsh\skills` 应出现各技能目录，每个目录内存在带 `name`/`description` frontmatter 的 `SKILL.md`。新会话里即可用对应技能名（kebab-case）加载。
