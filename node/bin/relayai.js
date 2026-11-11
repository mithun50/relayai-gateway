#!/usr/bin/env node
"use strict";

require("../src/cli")
  .main(process.argv.slice(2))
  .then((code) => {
    process.exitCode = code;
  })
  .catch((err) => {
    process.stderr.write(`relayai: ${err && err.message ? err.message : err}\n`);
    process.exitCode = 1;
  });
