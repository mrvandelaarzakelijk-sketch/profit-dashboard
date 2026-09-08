// Vercel serverless functie: haalt productgegevens (titel, prijs, valuta, afbeelding) op
// van leverancier-websites zoals Temu, 1688, AliExpress en Alibaba.
//
//   GET /api/scrape?url=https://www.temu.com/....html
//   ->  { ok, site, siteName, url, title, price, priceMax, currency, priceEur, image, fetchedAt }
//
// Werking: de pagina wordt opgehaald met browser-headers. Daarna worden achtereenvolgens
// JSON-LD (schema.org/Product), de ingebedde JSON van de site zelf (window.rawData op Temu,
// window.__INIT_DATA op 1688, window.runParams op AliExpress, ...) en de OpenGraph/meta-tags
// uitgelezen. Een nieuwe website voeg je toe aan SITES (alleen de sleutelnamen waar de site
// zijn titel/prijs/afbeelding in bewaart); alles wat daar niet in staat valt automatisch terug
// op de generieke JSON-LD/meta-extractie.
//
// Let op: Temu en 1688 blokkeren regelmatig verzoeken vanaf cloud-IP's (zoals Vercel) met een
// captcha- of loginpagina. Zet in dat geval een scrape-proxy (ScraperAPI, ScrapingBee, Zenrows,
// ...) in via de omgevingsvariabele SCRAPE_PROXY_URL, met {url} als plaatshouder, bijv.:
//   SCRAPE_PROXY_URL="https://api.scraperapi.com/?api_key=XXXX&render=true&url={url}"

const TIMEOUT_MS = 20000;            // max. wachttijd per pagina
const MAX_BYTES = 4 * 1024 * 1024;   // max. paginagrootte die we inlezen
const MAX_BLOBS = 40;                // max. aantal JSON-blokken dat we uit een pagina parsen
const MAX_REDIRECTS = 5;

// wisselkoersen -> euro (pas aan indien nodig)
const TO_EUR = { EUR: 1, USD: 0.86, GBP: 1.17, CNY: 0.12, HKD: 0.11, JPY: 0.0056, AUD: 0.57, CAD: 0.63 };

const BROWSER_HEADERS = {
  'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
  'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
  'Accept-Language': 'nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7',
  'Cache-Control': 'no-cache',
  'Upgrade-Insecure-Requests': '1',
};

// ---- Site-adapters -------------------------------------------------------------------------
// Per site: welke hostnamen erbij horen, de standaardvaluta en de sleutelnamen (in volgorde van
// voorkeur) waaronder titel, prijs, afbeelding en valuta in de ingebedde JSON van de pagina staan.
// minorUnits: kale getallen staan in centen (Temu) en worden door 100 gedeeld.
const SITES = [
  {
    id: '1688', name: '1688', currency: 'CNY',
    match: h => /(^|\.)1688\.com$/.test(h),
    // window.__INIT_DATA -> globalData.tempModel / skuModel / orderParamModel / images
    title: ['offerTitle', 'offerSubject', 'subject'],
    price: ['skuPriceScale', 'skuPriceScaleOriginal', 'skuRangePrices', 'priceRanges', 'currentPrices', 'refPrice', 'price'],
    image: ['fullPathImageURI', 'imageUrl', 'mainImage', 'offerImage'],
    currencyKeys: [],
  },
  {
    id: 'temu', name: 'Temu', currency: null,
    match: h => /(^|\.)temu\.com$/.test(h),
    // window.rawData -> store.goods / store.sku / store.currency
    title: ['goodsName', 'goods_name', 'goodsTitle'],
    price: ['priceStr', 'normalPriceStr', 'salePriceStr', 'skuPriceStr', 'linePriceStr', 'normalPrice', 'salePrice', 'skuPrice', 'price'],
    image: ['hdThumbUrl', 'thumbUrl', 'hd_thumb_url', 'thumb_url', 'imageUrl'],
    currencyKeys: ['currency', 'currencyCode', 'currency_code'],
    minorUnits: true,
  },
  {
    id: 'aliexpress', name: 'AliExpress', currency: 'USD',
    match: h => /(^|\.)aliexpress\.(com|us|ru)$/.test(h),
    // window.runParams -> data.productInfoComponent / priceComponent / imageComponent
    title: ['subject', 'productTitle', 'title'],
    price: ['minActivityAmount', 'minAmount', 'salePrice', 'skuActivityAmount', 'skuAmount', 'minPrice', 'formatedAmount', 'price'],
    image: ['imagePathList', 'imageUrl', 'mainImageUrl'],
    currencyKeys: ['currencyCode', 'currency'],
  },
  {
    id: 'alibaba', name: 'Alibaba', currency: 'USD',
    match: h => /(^|\.)alibaba\.com$/.test(h),
    // window.detailData -> productModel / priceModel
    title: ['subject', 'productTitle', 'title'],
    price: ['priceRange', 'formatPrice', 'minPrice', 'price'],
    image: ['imageUrl', 'mainImage', 'imgUrl'],
    currencyKeys: ['currency', 'currencyCode'],
  },
];
const GENERIC = { id: 'generic', name: 'Website', currency: null, title: [], price: [], image: [], currencyKeys: [] };

export function detectSite(hostname) {
  const h = String(hostname || '').toLowerCase();
  return SITES.find(s => s.match(h)) || GENERIC;
}

// ---- URL-validatie -------------------------------------------------------------------------
export function normalizeUrl(raw) {
  let s = String(raw || '').trim();
  if (!s) return null;
  if (/^[a-z][a-z0-9+.-]*:/i.test(s)) {
    if (!/^https?:\/\//i.test(s)) return null;          // ander protocol (ftp:, javascript:, ...)
  } else {
    s = 'https://' + s.replace(/^\/+/, '');              // geen protocol opgegeven
  }
  let u;
  try { u = new URL(s); } catch (e) { return null; }
  if (!/^https?:$/.test(u.protocol) || u.username || u.password) return null;
  u.hash = '';
  return u;
}

// Alleen publieke hostnamen: geen localhost, interne namen of IP-adressen (beveiliging tegen
// verzoeken naar het eigen netwerk).
export function isPublicHost(hostname) {
  const h = String(hostname || '').toLowerCase().replace(/\.$/, '');
  if (!h || h.includes(':') || /^\[.*\]$/.test(h)) return false;          // leeg / IPv6
  if (/^\d+\.\d+\.\d+\.\d+$/.test(h)) return false;                        // IPv4-literal
  if (h === 'localhost' || !h.includes('.')) return false;
  if (/\.(localhost|local|internal|lan|home|corp|intranet)$/.test(h)) return false;
  return true;
}

// ---- Pagina ophalen ------------------------------------------------------------------------
function buildProxyUrl(proxy, url) {
  if (proxy.includes('{url}')) return proxy.replace('{url}', encodeURIComponent(url));
  return proxy + (proxy.includes('?') ? '&' : '?') + 'url=' + encodeURIComponent(url);
}

async function readCapped(resp, max) {
  const body = resp.body;
  if (!body || typeof body.getReader !== 'function') {
    const buf = Buffer.from(await resp.arrayBuffer());
    return buf.subarray(0, max);
  }
  const reader = body.getReader();
  const chunks = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    total += value.length;
    if (total >= max) { reader.cancel().catch(() => {}); break; }
  }
  return Buffer.concat(chunks.map(c => Buffer.from(c))).subarray(0, max);
}

function decodeBody(bytes, contentType) {
  const head = bytes.subarray(0, 4096).toString('latin1');
  const m = /charset=["']?([\w-]+)/i.exec(contentType || '') || /<meta[^>]+charset=["']?([\w-]+)/i.exec(head);
  const cs = m ? m[1].toLowerCase() : 'utf-8';
  if (/^(gbk|gb2312|gb18030)$/.test(cs)) {
    try { return new TextDecoder('gbk').decode(bytes); } catch (e) { /* geen ICU -> utf-8 */ }
  }
  return bytes.toString('utf8');
}

async function fetchOnce(url, signal) {
  return fetch(url, { headers: BROWSER_HEADERS, redirect: 'manual', signal });
}

// Haalt de pagina op; volgt redirects zelf zodat elke tussenstap op een publieke host blijft.
export async function fetchPage(url) {
  const proxy = process.env.SCRAPE_PROXY_URL;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
  try {
    let current = proxy ? buildProxyUrl(proxy, url) : url;
    let resp = await fetchOnce(current, ctl.signal);
    let hops = 0;
    while (!proxy && [301, 302, 303, 307, 308].includes(resp.status) && hops < MAX_REDIRECTS) {
      const loc = resp.headers.get('location');
      if (!loc) break;
      const next = new URL(loc, current);
      if (!/^https?:$/.test(next.protocol) || !isPublicHost(next.hostname)) {
        throw new Error('redirect naar niet-toegestane host: ' + next.hostname);
      }
      current = next.href;
      hops += 1;
      resp = await fetchOnce(current, ctl.signal);
    }
    const bytes = await readCapped(resp, MAX_BYTES);
    const html = decodeBody(bytes, resp.headers.get('content-type'));
    return { status: resp.status, finalUrl: proxy ? url : current, html, viaProxy: !!proxy };
  } finally {
    clearTimeout(timer);
  }
}

// ---- Hulpfuncties: tekst -------------------------------------------------------------------
const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' };
function decodeEntities(s) {
  return String(s).replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (m, e) => {
    if (e[0] === '#') {
      const code = e[1].toLowerCase() === 'x' ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
      return Number.isFinite(code) ? String.fromCodePoint(code) : m;
    }
    return ENTITIES[e.toLowerCase()] ?? m;
  });
}
function cleanText(s) {
  if (typeof s !== 'string') return null;
  const t = decodeEntities(s).replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
  return t ? t.slice(0, 200) : null;
}
function isTitle(v) {
  if (typeof v !== 'string') return null;
  const t = cleanText(v);
  if (!t || t.length < 3 || /^https?:\/\//i.test(t)) return null;
  return t;
}

// ---- Hulpfuncties: prijzen -----------------------------------------------------------------
const NUM = '\\d+(?:[.,]\\d+)*';
const SYM = '(?:HK\\$|US\\s?\\$|CN¥|A\\$|C\\$|R\\$|RMB|USD|EUR|GBP|CNY|JPY|HKD|AUD|CAD|[€£$¥￥])';
const RE_SYM_BEFORE = new RegExp(SYM + '\\s*(' + NUM + ')(?:\\s*[-–~]\\s*' + SYM + '?\\s*(' + NUM + '))?', 'i');
const RE_SYM_AFTER = new RegExp('(' + NUM + ')(?:\\s*[-–~]\\s*(' + NUM + '))?\\s*' + SYM, 'i');
const RE_BARE = new RegExp('^\\s*(' + NUM + ')(?:\\s*[-–~]\\s*(' + NUM + '))?\\s*$');

// "1.234,56" / "1,234.56" / "12,5" / "2.50" -> getal
function toNumber(tok) {
  const lastComma = tok.lastIndexOf(','), lastDot = tok.lastIndexOf('.');
  let s = tok;
  if (lastComma >= 0 && lastDot >= 0) {
    s = lastComma > lastDot ? tok.replace(/\./g, '').replace(',', '.') : tok.replace(/,/g, '');
  } else if (lastComma >= 0) {
    const commas = (tok.match(/,/g) || []).length;
    const dec = tok.length - lastComma - 1;
    s = (commas === 1 && dec !== 3) ? tok.replace(',', '.') : tok.replace(/,/g, '');
  } else if (lastDot >= 0) {
    const dots = (tok.match(/\./g) || []).length;
    const dec = tok.length - lastDot - 1;
    if (dots > 1 || (dec === 3 && !tok.startsWith('0.'))) s = tok.replace(/\./g, '');
  }
  const n = parseFloat(s);
  return Number.isFinite(n) && n > 0 && n < 1e7 ? n : null;
}

export function currencyFromString(s) {
  if (typeof s !== 'string') return null;
  if (/HK\$|HKD/.test(s)) return 'HKD';
  if (/A\$|AUD/.test(s)) return 'AUD';
  if (/C\$|CAD/.test(s)) return 'CAD';
  if (/US\s?\$|USD/.test(s)) return 'USD';
  if (/CN¥|CNY|RMB|人民币|元/.test(s)) return 'CNY';
  if (/JP¥|JPY|円/.test(s)) return 'JPY';
  if (/€|EUR/.test(s)) return 'EUR';
  if (/£|GBP/.test(s)) return 'GBP';
  if (/[¥￥]/.test(s)) return 'CNY';
  if (/\$/.test(s)) return 'USD';
  return null;
}

// "¥2.50-3.80", "US $12.34", "12,99 €", "2.50" -> { min, max, currency }
export function parsePriceString(s) {
  if (typeof s !== 'string') return null;
  const str = s.replace(/\d+(?:[.,]\d+)?\s*%/g, '').replace(/\s+/g, ' ').trim();
  if (!str || str.length > 80 || /^https?:/i.test(str) || /\d{4}-\d{2}-\d{2}/.test(str)) return null;
  const m = RE_SYM_BEFORE.exec(str) || RE_SYM_AFTER.exec(str) || RE_BARE.exec(str);
  if (!m) return null;
  const min = toNumber(m[1]);
  if (min == null) return null;
  const max = m[2] ? toNumber(m[2]) : null;
  return { min, max: max != null && max > min ? max : null, currency: currencyFromString(str) };
}

const PRICE_OBJ_KEYS = ['formatedAmount', 'priceStr', 'value', 'amount', 'min', 'minPrice', 'price', 'skuPrice'];

// Haalt een prijs uit een getal, tekst, lijst of object; lijsten geven de laagste prijs terug.
export function priceFromAny(v, minorUnits, depth = 0) {
  if (v == null || depth > 3) return null;
  if (typeof v === 'number') {
    if (!Number.isFinite(v) || v <= 0 || v >= 1e7) return null;
    return { min: minorUnits && Number.isInteger(v) ? v / 100 : v, max: null, currency: null };
  }
  if (typeof v === 'string') return parsePriceString(v);
  if (Array.isArray(v)) {
    let out = null;
    for (const item of v.slice(0, 50)) {
      const p = priceFromAny(item, minorUnits, depth + 1);
      if (!p) continue;
      if (!out) out = { ...p };
      else {
        const hi = Math.max(out.max ?? out.min, p.max ?? p.min);
        out.min = Math.min(out.min, p.min);
        out.max = hi > out.min ? hi : null;
        out.currency = out.currency || p.currency;
      }
    }
    return out;
  }
  if (typeof v === 'object') {
    for (const k of PRICE_OBJ_KEYS) {
      if (v[k] == null) continue;
      const p = priceFromAny(v[k], minorUnits, depth + 1);
      if (p) {
        if (!p.currency && typeof v.currency === 'string') p.currency = v.currency.toUpperCase();
        if (!p.currency && typeof v.currencyCode === 'string') p.currency = v.currencyCode.toUpperCase();
        return p;
      }
    }
  }
  return null;
}

function isCurrency(v) {
  if (typeof v === 'string' && /^[A-Z]{3}$/.test(v.trim().toUpperCase())) return v.trim().toUpperCase();
  if (v && typeof v === 'object') return isCurrency(v.currency) || isCurrency(v.currencyCode) || isCurrency(v.code);
  return null;
}

// ---- Hulpfuncties: afbeeldingen ------------------------------------------------------------
function looksLikeImage(s) {
  return typeof s === 'string' && s.length < 1000 && /^(https?:)?\/\//i.test(s) &&
    /\.(jpe?g|png|webp|avif|gif)(\?|$)|image|img|photo|pic/i.test(s);
}
function firstImage(v, depth = 0) {
  if (v == null || depth > 2) return null;
  if (typeof v === 'string') return looksLikeImage(v) ? v : null;
  if (Array.isArray(v)) {
    for (const item of v.slice(0, 20)) { const r = firstImage(item, depth + 1); if (r) return r; }
    return null;
  }
  if (typeof v === 'object') {
    for (const k of ['url', 'src', 'contentUrl', 'imageUrl', 'fullPathImageURI', 'image']) {
      const r = firstImage(v[k], depth + 1); if (r) return r;
    }
  }
  return null;
}
function absUrl(img, base) {
  if (!img) return null;
  try {
    if (img.startsWith('//')) return 'https:' + img;
    const u = new URL(img, base);
    return /^https?:$/.test(u.protocol) ? u.href : null;
  } catch (e) { return null; }
}

// ---- JSON uit de pagina halen --------------------------------------------------------------
// Geeft het gebalanceerde {...} of [...] blok terug dat op positie `start` begint.
export function sliceBalanced(str, start) {
  const open = str[start];
  if (open !== '{' && open !== '[') return null;
  let depth = 0, inStr = false, quote = '', esc = false;
  for (let i = start; i < str.length; i++) {
    const ch = str[i];
    if (inStr) {
      if (esc) esc = false;
      else if (ch === '\\') esc = true;
      else if (ch === quote) inStr = false;
      continue;
    }
    if (ch === '"' || ch === "'") { inStr = true; quote = ch; continue; }
    if (ch === '{' || ch === '[') depth += 1;
    else if (ch === '}' || ch === ']') {
      depth -= 1;
      if (depth === 0) return str.slice(start, i + 1);
    }
  }
  return null;
}

function tryJson(txt) {
  try { return JSON.parse(txt); } catch (e) { return undefined; }
}

// Verzamelt alle JSON-blokken uit de pagina: <script type="application/ld+json">,
// <script type="application/json"> en window.xxx = {...} toekenningen.
export function collectJsonBlobs(html) {
  const blobs = [];
  const push = txt => {
    const v = tryJson(txt);
    if (v === undefined || v === null || typeof v !== 'object') return false;
    if (Array.isArray(v)) v.forEach(x => { if (x && typeof x === 'object') blobs.push(x); });
    else blobs.push(v);
    return true;
  };
  const scriptRe = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi;
  let m;
  while ((m = scriptRe.exec(html)) && blobs.length < MAX_BLOBS) {
    const attrs = m[1], body = m[2];
    if (/type\s*=\s*["']?application\/(ld\+)?json/i.test(attrs)) { push(body.trim()); continue; }
    if (!/window\./.test(body)) continue;
    const winRe = /window\.([A-Za-z_$][\w$]*)\s*=\s*(?=[[{])/g;
    let w;
    while ((w = winRe.exec(body)) && blobs.length < MAX_BLOBS) {
      const start = w.index + w[0].length;
      const slice = sliceBalanced(body, start);
      if (!slice || slice.length < 40) continue;
      if (!push(slice)) {
        // JS-objectliteral (geen geldige JSON): probeer de "data": {...} onderdelen apart
        const dataRe = /["']?data["']?\s*:\s*(?=[[{])/g;
        let d, found = 0;
        while ((d = dataRe.exec(slice)) && found < 3) {
          const sub = sliceBalanced(slice, d.index + d[0].length);
          if (sub && sub.length > 40 && push(sub)) { found += 1; dataRe.lastIndex = d.index + d[0].length + sub.length; }
        }
      }
      winRe.lastIndex = start + slice.length;
    }
  }
  return blobs;
}

// Breedte-eerst zoeken naar een sleutel; ondiepe treffers (het hoofdproduct) winnen van diepe
// (aanbevolen producten). `normalize` maakt van de gevonden waarde een bruikbaar resultaat of null.
function walkFind(root, key, normalize, maxNodes = 300000) {
  const queue = [root];
  let i = 0;
  while (i < queue.length && i < maxNodes) {
    const cur = queue[i++];
    if (!cur || typeof cur !== 'object') continue;
    if (!Array.isArray(cur) && Object.prototype.hasOwnProperty.call(cur, key)) {
      const v = normalize(cur[key]);
      if (v != null) return v;
    }
    for (const k in cur) {
      const v = cur[k];
      if (v && typeof v === 'object') queue.push(v);
    }
  }
  return null;
}
function findByKeys(blobs, keys, normalize) {
  for (const key of keys) {
    for (const blob of blobs) {
      const hit = walkFind(blob, key, normalize);
      if (hit != null) return hit;
    }
  }
  return null;
}

// ---- Extractie: JSON-LD (schema.org/Product) -----------------------------------------------
function isProductNode(n) {
  if (!n || typeof n !== 'object') return false;
  const t = n['@type'];
  return t === 'Product' || (Array.isArray(t) && t.includes('Product'));
}
function findProductNode(blob, depth = 0) {
  if (!blob || typeof blob !== 'object' || depth > 3) return null;
  if (isProductNode(blob)) return blob;
  const kids = Array.isArray(blob) ? blob : [blob['@graph'], blob.mainEntity, blob.itemListElement].filter(Boolean);
  for (const k of kids) {
    const list = Array.isArray(k) ? k : [k];
    for (const item of list) { const r = findProductNode(item && item.item ? item.item : item, depth + 1); if (r) return r; }
  }
  return null;
}
export function extractJsonLd(blobs) {
  for (const blob of blobs) {
    const p = findProductNode(blob);
    if (!p) continue;
    const out = { title: isTitle(p.name), image: firstImage(p.image), price: null, currency: null };
    const offers = Array.isArray(p.offers) ? p.offers : (p.offers ? [p.offers] : []);
    for (const o of offers) {
      if (!o || typeof o !== 'object') continue;
      const lo = priceFromAny(o.lowPrice ?? o.price, false);
      if (lo) {
        const hi = priceFromAny(o.highPrice, false);
        out.price = { min: lo.min, max: hi && hi.min > lo.min ? hi.min : lo.max, currency: lo.currency };
        out.currency = isCurrency(o.priceCurrency) || lo.currency;
        break;
      }
    }
    if (out.title || out.price) return out;
  }
  return { title: null, image: null, price: null, currency: null };
}

// ---- Extractie: meta-tags / OpenGraph ------------------------------------------------------
export function extractMeta(html) {
  const meta = {};
  const re = /<meta\b[^>]*>/gi;
  let m;
  while ((m = re.exec(html))) {
    const tag = m[0];
    const key = /(?:property|name|itemprop)\s*=\s*["']([^"']+)["']/i.exec(tag);
    const val = /content\s*=\s*["']([^"']*)["']/i.exec(tag);
    if (key && val && meta[key[1].toLowerCase()] === undefined) meta[key[1].toLowerCase()] = decodeEntities(val[1]);
  }
  const t = /<title[^>]*>([\s\S]*?)<\/title>/i.exec(html);
  const amount = meta['product:price:amount'] || meta['og:price:amount'] || meta['price'];
  const price = amount ? parsePriceString(amount) : null;
  return {
    title: isTitle(meta['og:title'] || meta['twitter:title'] || (t && t[1]) || ''),
    image: firstImage(meta['og:image:secure_url'] || meta['og:image'] || meta['twitter:image']),
    price,
    currency: isCurrency(meta['product:price:currency'] || meta['og:price:currency'] || meta['pricecurrency']) || (price && price.currency) || null,
  };
}

// ---- Extractie: site-specifieke JSON -------------------------------------------------------
function extractSite(blobs, site) {
  return {
    title: findByKeys(blobs, site.title, isTitle),
    image: findByKeys(blobs, site.image, v => firstImage(v)),
    price: findByKeys(blobs, site.price, v => priceFromAny(v, site.minorUnits)),
    currency: findByKeys(blobs, site.currencyKeys || [], isCurrency),
  };
}

// ---- Alles samenvoegen ---------------------------------------------------------------------
export function extractProduct(html, url, site) {
  const blobs = collectJsonBlobs(html);
  const ld = extractJsonLd(blobs);
  const own = extractSite(blobs, site);
  const meta = extractMeta(html);

  const title = ld.title || own.title || meta.title || null;
  const image = absUrl(ld.image || own.image || meta.image, url);
  const p = ld.price || own.price || meta.price || null;
  const currency = (p && p.currency) || ld.currency || own.currency || meta.currency || site.currency || null;
  const rate = currency ? TO_EUR[currency] : null;
  const r2 = n => Math.round(Math.round(n * 1e6) / 1e4) / 100;   // afronden op 2 decimalen zonder float-ruis
  const price = p ? r2(p.min) : null;
  const priceMax = p && p.max ? r2(p.max) : null;
  return {
    title, image, price, priceMax, currency,
    priceEur: price != null && rate ? r2(price * rate) : null,
    source: ld.price ? 'json-ld' : (own.price ? site.id : (meta.price ? 'meta' : null)),
  };
}

const BLOCK_MARKERS = [
  /_____tmd_____|\/punish\?|nc_1_n1z|nocaptcha|slidercaptcha/i,       // Alibaba / 1688 slider
  /cf-chl-|challenge-platform|just a moment/i,                        // Cloudflare
  /captcha|access denied|are you a (robot|human)|verify (that )?you are (a )?human/i,
  /bot detection|unusual traffic|security verification|verification required/i,
];
export function looksBlocked(html, status, finalUrl) {
  if ([401, 403, 407, 429, 503].includes(status)) return true;
  try {
    const u = new URL(finalUrl);
    if (/^(login|passport|auth)\./i.test(u.hostname) || /\/(login|passport|captcha|punish|verify)/i.test(u.pathname)) return true;
  } catch (e) { /* negeren */ }
  const head = html.slice(0, 200000);
  return BLOCK_MARKERS.some(re => re.test(head));
}

export default async function handler(req, res) {
  const raw = (req.query && req.query.url) || (req.body && req.body.url) || '';
  const target = normalizeUrl(raw);
  if (!target) {
    res.setHeader('Cache-Control', 'no-store');
    return res.status(400).json({ ok: false, error: raw ? 'ongeldige url' : 'url ontbreekt (gebruik ?url=...)' });
  }
  if (!isPublicHost(target.hostname)) {
    res.setHeader('Cache-Control', 'no-store');
    return res.status(400).json({ ok: false, error: 'alleen publieke websites zijn toegestaan' });
  }
  const site = detectSite(target.hostname);
  const base = { site: site.id, siteName: site.name, url: target.href, fetchedAt: new Date().toISOString() };
  try {
    const page = await fetchPage(target.href);
    const data = extractProduct(page.html, page.finalUrl, site);
    const found = data.price != null || data.title;
    if (data.price != null) {
      res.setHeader('Cache-Control', 's-maxage=300, stale-while-revalidate=600');
      return res.status(200).json({ ok: true, ...base, url: page.finalUrl, viaProxy: page.viaProxy, ...data });
    }
    res.setHeader('Cache-Control', 'no-store');
    const blocked = looksBlocked(page.html, page.status, page.finalUrl);
    let error = found ? 'geen prijs gevonden op de pagina' : 'geen productgegevens gevonden (status ' + page.status + ')';
    if (blocked) {
      error = site.name + ' blokkeert dit verzoek (captcha/login)';
      if (!page.viaProxy) error += ' — stel SCRAPE_PROXY_URL in om via een scrape-proxy te gaan';
    }
    return res.status(200).json({ ok: false, ...base, url: page.finalUrl, status: page.status, blocked, viaProxy: page.viaProxy, error, ...data });
  } catch (e) {
    res.setHeader('Cache-Control', 'no-store');
    const msg = e && e.name === 'AbortError' ? 'time-out bij het ophalen van de pagina' : String((e && e.message) || e);
    return res.status(200).json({ ok: false, ...base, error: msg });
  }
}
