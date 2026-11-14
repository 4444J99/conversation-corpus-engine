# KNOVV source-capture integration validation — 2026-11-02

## Scope and outcome

The source-capture implementation from PR #146 at
`8fb3032540a352238d99a9e814235ed9ad5ba2f6` was restored into an isolated Linux
workspace for full repository validation. The restoration included 645 files:
all application source, tests, workspace packages, build configuration, scripts,
and documentation. Each restored file was checked against its remote Git blob
identity. Personal intake data, backup payloads, and tool-local settings were
excluded; no live source account or browser profile was accessed.

The final validation repair passes the root lint command, every workspace build
in `build:all`, and the full Vitest suite. It changes test fixtures and toolchain
configuration; it does not change the capture implementation or production
redaction logic.

## Defects reproduced and corrected

| Observed failure | Evidence and correction |
|---|---|
| `npm ci` rejected the original manifest/lock combination | The root override selected Vite 7.3.5 while the original lock and web workspace selected Vite 8.2.1. Merged dependency PR #97 (`9d81ef35574b052bd8e3a440db672ba51956f3c7`) explicitly upgraded Vite to 8.2.1 but left the old override. The override now matches that existing version. |
| Claude and ChatGPT source tests could not parse | Existing redacted URL placeholders were unterminated string literals. Synthetic provider URLs with fixture IDs restore the original assertions. Both files were identical at the PR base and initial head. |
| Nine redaction tests failed | Inputs contained literal `[email redacted]` and `[phone redacted]` placeholders instead of strings matching the tested formats. Reserved `.invalid` addresses, fictional telephone numbers, and synthetic basic-auth credentials restore the intended cases without weakening assertions. The original test blob was identical at the PR base and initial head. |
| TypeScript 7 rejected `moduleResolution: node` | Root, contracts, mobile, and desktop configs now use the already-established web workspace's `bundler` resolution with ESNext emission. Mobile/desktop explicitly preserve their common source root for shared contracts; desktop explicitly selects its existing Node types. |
| Full web build failed with `vite/internal` not exported | The Vite 7 override conflicted with the existing React plugin's Vite 8 import. The corrected Vite 8.2.1 override exposes the required entry and the web build passes. |
| Workflows selected an unsupported Node version | The installed jsdom 30.0.1 package requires `^22.22.2 || ^24.15.0 || >=26.0.0`; other existing runtime packages also require Node 22+. The package now declares that range, four workflows select Node 24, and the deployment prerequisite is updated. Both Docker stages already use Node 26 and remain unchanged. |

The original full run produced 98 passing and 3 failing suites, with 1,484
passing and 9 failing tests; two failed suites had no collected tests because
of the syntax errors. The final full run collected and passed all 1,534 tests
in 101 suites.

The final lockfile preserves every original package entry, version, resolved
URL, integrity value, and native platform constraint. Its only change is the
three-line root Node engine declaration. No direct dependency version range was
changed; the Vite override was aligned with the version already approved and
locked in the repository.

## Executed verification

Environment: Linux, Node 24.22.1, npm 11.9.0, TypeScript 7.0.2, Vitest 4.1.10,
Vite 8.2.1. Tests used mock embeddings and disposable local database state.

| Check | Result |
|---|---|
| Clean `npm ci --ignore-scripts --offline --no-audit --no-fund` | Passed using the preserved final lock and previously downloaded npm cache. |
| `npm rebuild better-sqlite3 esbuild` | Passed; native dependencies were rebuilt after the clean install. |
| `npm run lint` | Passed. |
| `npm run build:all` | Passed: root application, contracts, React production build, mobile TypeScript shell, and desktop TypeScript shell. |
| `npm test -- --run` | 101 suites and 1,534 tests passed; final clean run completed in 15.32 seconds. |
| Independent agent review | No blocking findings; 144 tests passed across 10 focused suites on the final dependency state. Fixtures, module resolution, engine/workflow changes, lock preservation, and compiled source-manager/web-clipper imports were reviewed. No source was initialized during the import smoke. This is not a GitHub approval. |
| `git diff --check` | Passed. |

Temporary compiler outputs produced by the earlier failed workspace builds were
removed from the contracts source directory. The corrected build was rerun and
produced no untracked source outputs before the final full test run.

The web build emits non-blocking source-map and chunk-size warnings. No warning
thresholds or test exclusions were relaxed. This record does not claim native
Expo/Tauri packaging, live Apple Notes/Google/browser capture, new GitHub CI
execution, repository CodeQL configuration repair, or production deployment.

Source references: [capture PR #146](https://github.com/4444J99/my-knowledge-base/pull/146)
and [existing dependency upgrade #97](https://github.com/4444J99/my-knowledge-base/pull/97).

## Tested repair identities

All paths below have mode `100644`. These hashes identify the tested repair;
the enclosing verification document is an evidence addition.

| Path | Git blob SHA |
|---|---|
| `.github/workflows/ci.yml` | `a12381d09e281f8b8116ec23a2c7d37e3d6adfbc` |
| `.github/workflows/pages.yml` | `9215fcdde3f5f687aceb764641dca61d9fafea10` |
| `.github/workflows/release.yml` | `a4a097e8410df35095b70f279547964e86a22d28` |
| `.github/workflows/runtime-probe-dispatch.yml` | `656408b134b68558da7dab10e1651d24952eb75d` |
| `apps/desktop/tsconfig.json` | `843a342ac8bdeea07ab00f00cc63fe10bd075184` |
| `apps/mobile/tsconfig.json` | `6e31c6b58515ade4a1170698cf55ef4b9496b690` |
| `docs/DEPLOYMENT.md` | `dbd39c069b3ae167bda227a6a80f2d9baaa48f00` |
| `package-lock.json` | `a0d4ceec1e08f3200ad17deff6a06731c97680d5` |
| `package.json` | `574797e261cdbb9efbb0e7452347fea848e07faa` |
| `packages/contracts/tsconfig.json` | `f2171de67903e428062f3a2554d26498b803d295` |
| `src/redaction-service.test.ts` | `9973960235e3f40af9ad558bc37baea0658a7373` |
| `src/sources/chatgpt.test.ts` | `7020b2a0ee86428f16cc740161278a80cd4e5e75` |
| `src/sources/claude.test.ts` | `fd86163df9f19d673834b7649bfe1c0f115a88e9` |
| `tsconfig.json` | `b7508e7c33fe796899adcc4e4ba0b9669a004131` |
