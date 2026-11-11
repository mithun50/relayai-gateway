"use strict";
/** Tiny stderr logger shared by every module. Set log.quiet = true to silence it. */
const log = {
  quiet: false,
  line(level, msg) {
    if (!this.quiet) process.stderr.write(`${new Date().toTimeString().slice(0, 8)} ${level} ${msg}\n`);
  },
  info(msg) {
    this.line("INFO", msg);
  },
  warn(msg) {
    this.line("WARNING", msg);
  },
};

module.exports = { log };
