# Third-party dependency notice inventory

This source release does not vendor the dependency packages listed below. Its formal ST license is the unmodified PolyForm Noncommercial 1.0.0 in [LICENSE](LICENSE); that license does **not** relicense third-party packages, their bundled components, Python, SQLite, or separately obtained clients.

The following inventory records the exact distributions used for the Windows x86_64 / CPython 3.14 validation environment. License metadata and observed license files are evidence, not a replacement for full license and NOTICE texts or a complete legal audit. A future wheel bundle, executable, container, or other combined distribution must satisfy each applicable redistribution obligation separately.

| Package | Version | Distribution-declared license / observed qualification |
| --- | --- | --- |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| attrs | 26.1.0 | MIT |
| certifi | 2026.7.22 | MPL-2.0; Mozilla-derived certificate data retains its terms |
| cffi | 2.1.1 | MIT-0; inspect per-file exceptions when redistributing |
| click | 8.5.0 | BSD-3-Clause |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause; retain applicable bundled notices |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| httpx-sse | 0.4.3 | MIT |
| idna | 3.19 | BSD-3-Clause |
| jsonschema | 4.26.0 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| mcp | 1.29.0 | MIT |
| pycparser | 3.0 | BSD-3-Clause |
| pydantic | 2.13.4 | MIT |
| pydantic-core | 2.46.4 | MIT |
| pydantic-settings | 2.15.0 | MIT |
| pyjwt | 2.13.0 | MIT |
| python-dotenv | 1.2.3 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| pywin32 | 312 | Metadata says PSF; actual `win32/License.txt` has BSD-style source/binary notice and non-endorsement conditions, with per-file exceptions |
| referencing | 0.37.0 | MIT |
| rpds-py | 2026.6.3 | MIT |
| sse-starlette | 3.4.11 | BSD-3-Clause |
| starlette | 1.6.0 | BSD-3-Clause |
| typing-extensions | 4.16.0 | PSF-2.0 |
| typing-inspection | 0.4.4 | MIT |
| uvicorn | 0.52.4 | BSD-3-Clause |

Actual wheel hashes and registry origin domains are recorded in [docs/dependency-inventory.json](docs/dependency-inventory.json); the platform-specific complete hash lock is [requirements-windows-py314.lock](requirements-windows-py314.lock). License files were located in all 30 installed distributions. Selected actual files were read for MCP, HTTPX, certifi, cffi, cryptography, and pywin32; locating the remainder is not represented as a full legal review of every bundled subcomponent.

## Linux dependency baseline

The separate [Linux x86_64 / CPython 3.12 hash lock](requirements-linux-py312.lock) contains 29 distributions from the existing Linux validation baseline. Its selected cryptography wheel requires glibc 2.34 or later; Ubuntu 24.04 / CPython 3.12.3 is the recorded installation and L25 regression environment. This is a separate platform baseline, not a conversion of the Windows wheels.

| Package | Linux version | Distribution-declared license |
| --- | --- | --- |
| anyio | 4.14.2 | MIT |
| pydantic | 2.13.5 | MIT |
| pydantic-core | 2.46.5 | MIT |
| sse-starlette | 3.4.8 | BSD-3-Clause |

These four versions differ from the Windows table. The other 25 shared package versions match that table; Windows-only pywin32 is absent from the Linux lock. Platform-specific wheels have their own hashes even when package versions match. The original direct-dependency requirements file pins the Windows baseline and must not be layered over the Linux lock.

The public [Linux inventory](docs/dependency-inventory-linux-py312.json) records exact package versions, wheel names, SHA-256 values, official PyPI links and available license metadata. The selected wheel hashes were checked against official PyPI artifacts and the recorded offline installation; the public inventory includes no deployment paths, credentials or host identifiers.

LICENSE/COPYING texts were located in all 29 Linux wheels, and declared license-file presence was checked. This Linux check records declarations and file existence; it does not claim that every license text or bundled component received a full legal review. Third-party terms remain independent of ST's license. No third-party wheels or installed package directories are included in this source release.

For reference, the upstream [MCP SDK MIT text](https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/LICENSE), [HTTPX BSD text](https://raw.githubusercontent.com/encode/httpx/master/LICENSE.md), and [certifi notice](https://raw.githubusercontent.com/certifi/python-certifi/master/LICENSE) explain independent rights. These links can evolve; the installed exact-version distributions and their texts, not current branch labels alone, govern the artifacts you actually use.

## Clients and source provenance

The ST gateway implements Python-side protocol adaptation. The release does not include the RikkaHub native client or its APK, Kotlin/Java code, UI resources, or modified frontend. RikkaHub's inspected revision has its own [GNU AGPL v3 license](https://raw.githubusercontent.com/rikkahub/rikkahub/12ee935e0b0063dc3194d5145248795e071393cc/LICENSE). Interface compatibility does not relicense that client and is not a license to copy its implementation into this package.

See [SOURCE-PROVENANCE.md](SOURCE-PROVENANCE.md) for what was actually checked and what remains outside the audit. If previously unidentified borrowed material is found, preserve its notices, verify permissions, and correct the release; do not assume absence of an attribution header proves original authorship.
