Vendored front-end libraries (no CDN, no build step):

- preact 10.24.3: preact.mjs (dist/preact.module.js), preact-hooks.mjs (hooks/dist/hooks.module.js). MIT, see LICENSE-preact.txt.
- htm 3.1.1: htm.mjs (dist/htm.module.js). Apache-2.0, see LICENSE-htm.txt.

The import map in ../index.html maps the bare specifiers "preact", "preact/hooks" and "htm" to these files.
