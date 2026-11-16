// Engine text to inert display text: the TUI's rules (core/recursion_core/tui/sanitize.py), for a window.
//
// Drawing text with textContent already keeps markup out. These rules also keep out what text alone can
// do: escape and control sequences, bidi controls and invisible characters never reach the screen; each
// becomes a visible marker such as [OSC 52] in white square brackets, so nothing is hidden either. Held
// to desktop/tests/fixtures/sanitize.json, which is checked against the TUI's own function.
// Code points are written as numbers on purpose: no invisible character appears in this file.

const MAX_SEQ = 4096;
const EXTRA = new Set([0x034f, 0x115f, 0x3164]);
const STRING_INTRO = new Map([["]", "OSC"], ["P", "DCS"], ["X", "SOS"], ["^", "PM"], ["_", "APC"]]);
const C1_STRING = new Map([[0x9d, "OSC"], [0x90, "DCS"], [0x98, "SOS"], [0x9e, "PM"], [0x9f, "APC"]]);
const NAMED = new Map([[0x07, "BEL"], [0x08, "BS"], [0x7f, "DEL"], [0x0b, "VT"], [0x0c, "FF"], [0x18, "CAN"], [0x1a, "SUB"]]);
const INVISIBLE = /^[\p{Cf}\p{Co}\p{Cn}]$/u;
const OPEN = String.fromCodePoint(0x27e6);
const CLOSE = String.fromCodePoint(0x27e7);
const ESC = 0x1b;
const BEL = 0x07;
const ST = 0x9c;
const CAN = 0x18;
const SUB = 0x1a;

const GROUND = 0;
const AFTER_ESC = 1;
const ESC_INT = 2;
const CSI = 3;
const STR = 4;
const STR_ESC = 5;

export function marker(name) {
  return OPEN + name + CLOSE;
}

function isMarked(o, ch) {
  if (o < 0x20 || o === 0x7f || (o >= 0x80 && o <= 0x9f)) return true;
  if ((o >= 0xfe00 && o <= 0xfe0f) || (o >= 0xe0100 && o <= 0xe01ef)) return true;
  if (o < 0x80) return false;
  if (EXTRA.has(o)) return true;
  return INVISIBLE.test(ch);
}

function hex(o, width) {
  return o.toString(16).toUpperCase().padStart(width, "0");
}

function charMarker(o) {
  if (NAMED.has(o)) return marker(NAMED.get(o));
  if (o < 0x20) return marker("^" + String.fromCharCode(o + 0x40));
  if (o >= 0x80 && o <= 0x9f) return marker("C1 " + hex(o, 2));
  return marker("U+" + hex(o, 4));
}

export class OutputSanitizer {
  constructor() {
    this.state = GROUND;
    this.kind = "";
    this.buf = [];
    this.len = 0;
    this.cr = false;
  }

  feed(text) {
    const out = [];
    for (const ch of text) this.step(ch, out);
    return out.join("");
  }

  flush() {
    const out = [];
    if (this.cr) {
      out.push(marker("CR"));
      this.cr = false;
    }
    if (this.state !== GROUND) this.end(out, "unterminated");
    return out.join("");
  }

  step(ch, out) {
    const o = ch.codePointAt(0);
    if (this.cr) {
      this.cr = false;
      if (o === 0x0a) {
        out.push("\n");
        return;
      }
      out.push(marker("CR"));
    }
    switch (this.state) {
      case GROUND:
        this.ground(o, ch, out);
        break;
      case AFTER_ESC:
        this.afterEsc(o, ch, out);
        break;
      case ESC_INT:
        if (o === CAN || o === SUB) this.end(out, "aborted");
        else if (o >= 0x20 && o <= 0x2f) this.grow(ch, out);
        else this.end(out);
        break;
      case CSI:
        if (o === CAN || o === SUB) this.end(out, "aborted");
        else if (o === ESC) {
          this.end(out, "aborted");
          this.begin(AFTER_ESC, "ESC");
        } else if (o >= 0x40 && o <= 0x7e) this.end(out);
        else this.grow(ch, out);
        break;
      case STR:
        if ((o === BEL && this.kind === "OSC") || o === ST) this.end(out);
        else if (o === ESC) this.state = STR_ESC;
        else if (o === CAN || o === SUB) this.end(out, "aborted");
        else this.grow(ch, out);
        break;
      default: // STR_ESC: ESC \ is ST; any other ESC ends the string and starts a new escape
        if (ch === "\\") this.end(out);
        else {
          this.end(out, "aborted");
          this.begin(AFTER_ESC, "ESC");
          this.afterEsc(o, ch, out);
        }
    }
  }

  ground(o, ch, out) {
    if (!isMarked(o, ch)) out.push(ch);
    else if (o === 0x0a) out.push("\n");
    else if (o === 0x09) out.push("    ");
    else if (o === 0x0d) this.cr = true;
    else if (o === ESC) this.begin(AFTER_ESC, "ESC");
    else if (o === 0x9b) this.begin(CSI, "CSI");
    else if (C1_STRING.has(o)) this.begin(STR, C1_STRING.get(o));
    else out.push(charMarker(o));
  }

  afterEsc(o, ch, out) {
    if (ch === "[") this.begin(CSI, "CSI");
    else if (STRING_INTRO.has(ch)) this.begin(STR, STRING_INTRO.get(ch));
    else if (o === ESC) {
      this.end(out);
      this.begin(AFTER_ESC, "ESC");
    } else if (o === CAN || o === SUB) this.end(out, "aborted");
    else if (o >= 0x20 && o <= 0x2f) {
      this.state = ESC_INT;
      this.grow(ch, out);
    } else this.end(out);
  }

  begin(state, kind) {
    this.state = state;
    this.kind = kind;
    this.buf = [];
    this.len = 0;
  }

  grow(ch, out) {
    this.len += 1;
    if (this.buf.length < 8) this.buf.push(ch);
    if (this.len > MAX_SEQ) this.end(out, "overlong");
  }

  end(out, note = "") {
    let name = this.kind;
    if (name === "OSC") {
      const num = this.buf.join("").split(";", 1)[0];
      if (/^[0-9]+$/.test(num)) name = "OSC " + num;
    }
    out.push(marker(note ? name + " " + note : name));
    this.begin(GROUND, "");
  }
}

export function sanitize(text) {
  const s = new OutputSanitizer();
  return s.feed(text) + s.flush();
}

// What Python's str() gives for a JSON value, so a row reads exactly as the CLI's and the TUI's do. Held to
// desktop/tests/fixtures/pystr.json, which is written by Python and checked against str() and the TUI's field().
//
// What JSON.parse has already dropped cannot be given back, and is not guessed at. A number written 2.0 or
// 1e15 in the answer parses to the same value as 2 or 1000000000000000, so a whole number within 2**53 reads
// as an integer, as Python reads the plain form, and Python's "2.0" is not shown; -0.0 reads as "0". An
// integer past 2**53 has already lost digits and reads as Python's float of the same value. An object lists
// integer-like keys first, where Python keeps the text's order. The fixture's "lost" list names each of these.
export function pyStr(value) {
  if (typeof value === "string") return value;
  return pyRepr(value);
}

function pyRepr(value) {
  if (value === null || value === undefined) return "None";
  if (value === true) return "True";
  if (value === false) return "False";
  if (typeof value === "number") return Number.isSafeInteger(value) ? String(value) : pyFloat(value);
  if (typeof value === "string") return pyText(value);
  if (Array.isArray(value)) return "[" + value.map(pyRepr).join(", ") + "]";
  return "{" + Object.entries(value).map(([k, v]) => pyRepr(k) + ": " + pyRepr(v)).join(", ") + "}";
}

// Python's repr() of a float: the shortest digits that read back as the same value (as JavaScript's own), in
// fixed form from 1e-4 up to 1e16 with ".0" on a whole value, and otherwise as d.ddde-XX with a sign and at
// least two exponent digits.
function pyFloat(x) {
  if (Number.isNaN(x)) return "nan";
  if (x === Infinity) return "inf";
  if (x === -Infinity) return "-inf";
  const [mantissa, power] = x.toExponential().split("e");
  const exp = Number(power);
  if (exp < -4 || exp >= 16) return mantissa + "e" + (exp < 0 ? "-" : "+") + String(Math.abs(exp)).padStart(2, "0");
  const sign = mantissa.startsWith("-") ? "-" : "";
  const digits = mantissa.replace("-", "").replace(".", "");
  if (exp < 0) return sign + "0." + "0".repeat(-exp - 1) + digits;
  if (digits.length > exp + 1) return sign + digits.slice(0, exp + 1) + "." + digits.slice(exp + 1);
  return sign + digits + "0".repeat(exp + 1 - digits.length) + ".0";
}

// Python's repr() of a str: double quotes only when the text holds a single quote and no double quote; a
// backslash and the chosen quote escaped; tab, newline and carriage return by name; any other character
// Python does not print (controls, format and private-use characters, surrogates, unassigned code points,
// separators other than the space) as \xhh, \uhhhh or \Uhhhhhhhh.
const PY_UNPRINTABLE = /^[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}\p{Zs}]$/u;
const PY_NAMED = new Map([["\t", "\\t"], ["\n", "\\n"], ["\r", "\\r"]]);

function pyText(text) {
  const quote = text.includes("'") && !text.includes('"') ? '"' : "'";
  let out = quote;
  for (const ch of text) {
    const o = ch.codePointAt(0);
    if (ch === quote || ch === "\\") out += "\\" + ch;
    else if (PY_NAMED.has(ch)) out += PY_NAMED.get(ch);
    else if (ch === " " || !PY_UNPRINTABLE.test(ch)) out += ch;
    else if (o <= 0xff) out += "\\x" + o.toString(16).padStart(2, "0");
    else if (o <= 0xffff) out += "\\u" + o.toString(16).padStart(4, "0");
    else out += "\\U" + o.toString(16).padStart(8, "0");
  }
  return out + quote;
}

// A value shown on one row: sanitized, and a newline shown as a marker.
export function field(value) {
  return sanitize(pyStr(value)).replaceAll("\n", marker("LF"));
}
