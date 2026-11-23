// Patents: https://sdk.idside.eu/patents
// This software may be covered by the patents and applications listed there.

/**
 * Check-digit and structure validators named by the catalogue.
 *
 * Each takes the matched value (as read in the shadow) and returns how many of
 * its characters are the value — its whole length, or for an IBAN the part up
 * to its country's length — or `undefined` when the value fails. Python's twin
 * is `_cleaning/validators.py`; the shared vectors hold both.
 */

import { TABLES } from "./rules.ts";

const NETWORKS = TABLES.card_networks.map((network) => ({
  low: network.from,
  high: network.to,
  lengths: new Set(network.lengths),
}));
const COUNTRIES = new Set(TABLES.country_codes);
const VAT_ALPHABET = "0123456789" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ".replace(/[IO]/g, "");

function digitsOf(value: string): string {
  return value.replace(/[^0-9]/g, "");
}

/** ASCII digits only, at least one. */
function allDigits(text: string): boolean {
  return /^[0-9]+$/.test(text);
}

function whole(ok: boolean, value: string): number | undefined {
  return ok ? value.length : undefined;
}

export function luhn(digits: string): boolean {
  let total = 0;
  for (let index = 0; index < digits.length; index++) {
    let value = digits.charCodeAt(digits.length - 1 - index) - 48;
    if (index % 2) {
      value *= 2;
      if (value > 9) value -= 9;
    }
    total += value;
  }
  return total % 10 === 0;
}

export function card(value: string): number | undefined {
  const digits = digitsOf(value);
  if (!luhn(digits)) return undefined;
  for (const { low, high, lengths } of NETWORKS) {
    const prefix = Number(digits.slice(0, low.length));
    if (lengths.has(digits.length) && Number(low) <= prefix && prefix <= Number(high)) return value.length;
  }
  return undefined;
}

/** `text` (digits and letters, A=10…Z=35) read as a number, modulo 97. */
function mod97(text: string): number {
  let remainder = 0;
  for (const char of text) {
    const value = parseInt(char, 36);
    remainder = (remainder * (value > 9 ? 100 : 10) + value) % 97;
  }
  return remainder;
}

export function iban(value: string): number | undefined {
  const compact = value.replace(/[ -]/g, "").toUpperCase();
  const length = TABLES.iban_lengths[compact.slice(0, 2)];
  if (length === undefined || compact.length < length) return undefined;
  const candidate = compact.slice(0, length);
  if (mod97(candidate.slice(4) + candidate.slice(0, 4)) !== 1) return undefined;
  // How many characters of the value hold those `length` letters and digits.
  const positions: number[] = [];
  for (let index = 0; index < value.length; index++) if (value[index] !== " " && value[index] !== "-") positions.push(index);
  const cut = positions[length - 1] + 1;
  return cut === value.length || value[cut] === " " || value[cut] === "-" ? cut : undefined;
}

export function bic(value: string): number | undefined {
  return whole(COUNTRIES.has(value.slice(4, 6)), value);
}

export function nir(value: string): number | undefined {
  const compact = value.replace(/[ .-]/g, "").toUpperCase();
  if (compact.length !== 15) return undefined;
  const department = compact.slice(5, 7);
  let body = compact.slice(0, 13);
  if (department === "2A") body = body.slice(0, 5) + "19" + body.slice(7);
  else if (department === "2B") body = body.slice(0, 5) + "18" + body.slice(7);
  if (!allDigits(body) || !allDigits(compact.slice(13))) return undefined;
  return whole(97 - (Number(body) % 97) === Number(compact.slice(13)), value);
}

export function siren(value: string): number | undefined {
  const digits = digitsOf(value);
  return whole(digits.length === 9 && luhn(digits), value);
}

export function siret(value: string): number | undefined {
  const digits = digitsOf(value);
  if (digits.length !== 14 || !luhn(digits.slice(0, 9))) return undefined;
  if (digits.startsWith("356000000") && digits !== "35600000000048") {
    // La Poste: its establishments do not follow Luhn; their digits sum to a
    // multiple of 5 instead.
    let sum = 0;
    for (const char of digits) sum += char.charCodeAt(0) - 48;
    return whole(sum % 5 === 0, value);
  }
  return whole(luhn(digits), value);
}

export function frVat(value: string): number | undefined {
  const compact = value.replace(/ /g, "").toUpperCase().slice(2);
  const key = compact.slice(0, 2);
  const number = compact.slice(2);
  if (number.length !== 9 || !allDigits(number)) return undefined;
  if (number.slice(0, 3) !== "000" && !luhn(number)) return undefined;
  if (allDigits(key)) return whole(Number(key) === Number(number + "12") % 97, value);
  const check = allDigits(key[0])
    ? VAT_ALPHABET.indexOf(key[0]) * 24 + VAT_ALPHABET.indexOf(key[1]) - 10
    : VAT_ALPHABET.indexOf(key[0]) * 34 + VAT_ALPHABET.indexOf(key[1]) - 100;
  return whole((Number(number) + 1 + Math.floor(check / 11)) % 11 === check % 11, value);
}

function ipv4(value: string, allowPrefix: boolean): boolean {
  const slash = value.indexOf("/");
  const address = slash < 0 ? value : value.slice(0, slash);
  if (slash >= 0) {
    const prefix = value.slice(slash + 1);
    if (!(allowPrefix && allDigits(prefix) && Number(prefix) <= 32)) return false;
  }
  const parts = address.split(".");
  return parts.length === 4 && parts.every((part) => allDigits(part) && part.length <= 3 && Number(part) <= 255);
}

function ipv6(value: string): boolean {
  if (value.split("::").length > 2 || !/[0-9]/.test(value)) return false;
  let groups: string[];
  let compressed: boolean;
  if (value.includes("::")) {
    const [head, tail] = value.split("::");
    groups = [...(head ? head.split(":") : []), ...(tail ? tail.split(":") : [])];
    compressed = true;
  } else {
    groups = value.split(":");
    compressed = false;
  }
  let count = groups.length;
  if (groups.length > 0 && groups[groups.length - 1].includes(".")) {
    if (!ipv4(groups[groups.length - 1], false)) return false;
    groups = groups.slice(0, -1);
    count += 1;
  }
  if (!groups.every((group) => /^[0-9a-fA-F]{1,4}$/.test(group))) return false;
  if (!compressed) return count === 8;
  // `::1`, `::2` or `1::2` are as often a slice of code (`items[1::2]`) as an
  // address: a compressed address needs a group of three hex digits or more,
  // or an IPv4 tail.
  return count <= 7 && (count > groups.length || groups.some((group) => group.length >= 3));
}

export function ip(value: string): number | undefined {
  if (value.includes(":")) return whole(ipv6(value), value);
  return whole(ipv4(value, true), value);
}

export function phone(value: string): number | undefined {
  const digits = digitsOf(value);
  let count: number;
  if (value.startsWith("+")) count = digits.length;
  else if (value.startsWith("00")) count = digits.length - 2;
  else return value.length; // a national French number: its pattern is exact
  return whole(9 <= count && count <= 15, value);
}

export function plate(value: string): number | undefined {
  // The three digits of the plate, not those of a department after it.
  return whole(digitsOf(value).slice(0, 3) !== "000", value);
}

export const VALIDATORS: Readonly<Record<string, (value: string) => number | undefined>> = {
  bic,
  card,
  fr_vat: frVat,
  iban,
  ip,
  nir,
  phone,
  plate,
  siren,
  siret,
};
