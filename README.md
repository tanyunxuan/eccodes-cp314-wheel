# eccodes-cp314-wheel

为 **CPython 3.14** 重新打包的 ECMWF [`eccodes`](https://pypi.org/project/eccodes/) 原生库 wheel（Windows / Linux x86_64）。

本仓库**不编译任何 C 源码**，也**不修改 eccodes 的任何 Python 源码**：直接从官方 cp313 binary wheel 中取出随包发布的 ecCodes 原生库（纯 C ABI，与 Python 版本无关），替换掉其中与 CPython 版本绑定的一个极小“胶水”扩展，重写 wheel tag 后打成 cp314 wheel，并在 Python 3.14 上用 `eccodes` + `cfgrib` + `xarray` 做真实 GRIB2 读写验证，验证通过才发布到 GitHub Releases。

> 非 ECMWF 官方项目。ecCodes 与 eccodes Python 绑定的版权归 ECMWF，遵循 Apache License 2.0，wheel 内保留了官方的 LICENSE 与第三方库 license 清单。

## 背景：为什么需要这个仓库

官方 `eccodes` PyPI 包的关键事实（2026-09 调查）：

| 平台 | 官方 cp313 binary wheel | 官方 cp314 支持 |
| --- | --- | --- |
| Windows (`win_amd64`) | 有，跟随最新版本（当前 2.48.0） | **无** |
| Linux (`manylinux_2_28_x86_64`) | 有，但最新只到 **2.42.0**；2.43.0 起官方停发 Linux binary wheel | 通过新拆分包 [`eccodeslib`](https://pypi.org/project/eccodeslib/) 提供（已有 cp314 wheel，2.48.2.x） |
| 纯 Python 绑定 | `gribapi` 主体为纯 Python（通过 cffi ABI 模式 `dlopen` 原生库） | — |

官方 wheel 里唯一与 CPython 版本绑定的东西有两个：

1. wheel tag（`cp313-cp313-*`）；
2. `eccodes/_eccodes` —— 一个**约 1 KB 的手写“胶水”扩展**（官方连 C++ 源码 `_eccodes.cc` 都放在 wheel 里）。它只做两件事：
   - 利用扩展的导入表让操作系统加载器解析随包原生库（Windows 上的 DLL 依赖图 / Linux 上 auditwheel 改名后的兄弟 `.so`）；
   - 提供 `versions() -> {"eccodes": ECCODES_VERSION_STR}`。

本仓库用一个纯 Python（`ctypes` + 包元数据）的等价垫片 [`tools/_eccodes_shim.py`](tools/_eccodes_shim.py) 取代该扩展，其余文件（包括全部 `.py`、头文件、原生库、license 文件）**逐字节照搬**官方 wheel。

## 安装

> 建议 Python 3.14.0+。wheel 不发布到 PyPI，避免与官方 `eccodes` 包名冲突；通过 GitHub Releases 直链安装。

### Windows（基于官方 eccodes 2.48.0）

```powershell
pip install https://github.com/tanyunxuan/eccodes-cp314-wheel/releases/download/v2.48.0/eccodes-2.48.0-cp314-cp314-win_amd64.whl
```

### Linux x86_64（基于官方 eccodes 2.42.0 manylinux wheel）

```bash
pip install https://github.com/tanyunxuan/eccodes-cp314-wheel/releases/download/v2.42.0/eccodes-2.42.0-cp314-cp314-manylinux_2_28_x86_64.whl
```

后续上游发布新版本后，每周自动任务会自动跟随并发布新的 Release；请以 [Releases 页面](https://github.com/tanyunxuan/eccodes-cp314-wheel/releases) 的实际文件名为准。

随 GRIB 读取栈一起安装（推荐，含 cfgrib 验证过的组合）：

```bash
pip install <上面的 wheel URL> cfgrib xarray
```

### Linux 的官方替代方案

Linux 上 ECMWF 已经把原生库拆到 `eccodeslib` 并**官方提供 cp314 wheel**，因此也可以完全不用本仓库：

```bash
pip install eccodes cfgrib xarray
# Linux 上会自动拉取 eccodeslib 的 cp314 manylinux wheel
```

本仓库 CI 中有一个 `verify-official-linux` 任务，每次运行都会用同一个验收脚本验证这条官方路径仍然可用。

## 重打包到底改了什么

对官方 cp313 wheel：

1. 删除 `eccodes/_eccodes.cp313-*.pyd` / `eccodes/_eccodes.cpython-313-*.so`（保留官方的 `_eccodes.cc`）；
2. 写入纯 Python 垫片 `eccodes/_eccodes.py`（内容即 [`tools/_eccodes_shim.py`](tools/_eccodes_shim.py)）；
3. `*.dist-info/WHEEL` 中的 `Tag: cp313-cp313-*` 改为 `cp314-cp314-*`；
4. 按 PEP 427 重新生成 `RECORD`（排序、含自身条目）；
5. wheel 文件名同步改名。

`METADATA`（依赖声明）、`gribapi/`、所有原生库均未改动。脚本见 [`tools/repack.py`](tools/repack.py)，只有标准库依赖。

## 验证（每次 CI 必跑，失败不发布）

验收脚本 [`tests/verify_grib2.py`](tests/verify_grib2.py) 在 Windows 与 Linux 的 Python 3.14 环境中执行：

1. `from gribapi.bindings import library_path`，断言它真实指向随包原生库且文件存在；
2. 断言加载的是纯 Python 垫片 `_eccodes.py`，并调用 `versions()`；
3. `eccodes.codes_new_from_samples("regular_ll_sfc_grib2", eccodes.CODES_PRODUCT_GRIB)` 合成一个 12×6 的 2 米气温 GRIB2 字段并写入临时文件；
4. `xarray.open_dataset(path, engine="cfgrib")` 读回，逐点比对数值（atol=1e-4）。

完整日志见每个 Release 附带的 `verify-*.log`，以及 GitHub Actions 的运行记录。

## CI 工作流

[`.github/workflows/repack.yml`](.github/workflows/repack.yml)：

- **每周一 06:17 UTC** 自动运行，也支持 `workflow_dispatch` 手动触发；
- `discover`：查 PyPI JSON API，按平台选出“提供 cp313 wheel 的最新版本”（Windows 跟最新版，Linux 跟仍有 manylinux wheel 的最新版）；
- `repack`：下载官方 wheel → 重打包（zip 操作与平台无关，统一在 Ubuntu 上执行）；
- `verify`：矩阵在 `windows-latest` / `ubuntu-latest` 的 Python 3.14 上安装并跑验收；
- `verify-official-linux`：验证官方 `eccodes` + `eccodeslib` cp314 路径；
- `release`：全部验证通过后，按版本创建/更新 GitHub Release 并上传 wheel 与日志；已存在的相同资产自动跳过，因此每周重复运行是幂等的。

本地手动重打包：

```bash
python tools/repack.py discover --matrix-file matrix.json
python tools/repack.py repack --wheel eccodes-X.Y.Z-cp313-cp313-win_amd64.whl --out-dir dist
```

## 免责声明

- 本仓库与 ECMWF 无隶属关系；如遇 ecCodes 本身的问题请反馈给上游。
- 重打包 wheel 仅用于在官方发布 cp314 wheel 之前过渡；官方一旦覆盖对应平台，建议迁回官方包。
