# Third-party notices

NekoAgent's own source and the owner-authored optional Xiaofei Starter Pack are
licensed under Apache-2.0. The following third-party components have separate
licenses and notices:

| Component | Version / location | License | Notice handling |
| --- | --- | --- | --- |
| Anime.js | v4.5.0, `admin/anime.umd.min.js` | MIT | The bundled upstream notice is preserved in `admin/animejs.LICENSE.md`. |
| PyYAML | 6.0.2, declared in `requirements.lock` | MIT | Installed by each deployer; upstream project: https://github.com/yaml/pyyaml |

AstrBot and Codex are optional external runtimes. This repository contains an
adapter and integration documentation, not their binaries, packages, account
credentials or login state. Their licenses and terms apply when a deployer
chooses to install or use them.

Before adding a dependency, web asset, font, model asset or template, update
this inventory and preserve any required copyright and license text.
