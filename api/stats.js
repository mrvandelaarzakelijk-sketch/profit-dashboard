// Vercel serverless functie: haalt orders + refunds op uit Shopify (FR + UK)
// Nieuwe Shopify-flow (2026): Client ID + Secret -> access token (client credentials grant).

const API_VERSION = '2026-01';
const COGS_RATE = 0.29;
const GBP_TO_EUR = 1.17; // wisselkoers pond -> euro (pas aan indien nodig)

const STORES = {
  fr: {
    domain: process.env.FR_SHOP_DOMAIN,
    clientId: process.env.FR_CLIENT_ID,
    clientSecret: process.env.FR_CLIENT_SECRET,
  },
  uk: {
    domain: process.env.UK_SHOP_DOMAIN,
    clientId: process.env.UK_CLIENT_ID,
    clientSecret: process.env.UK_CLIENT_SECRET,
  },
};

const tokenCache = {};

async function getAccessToken(store) {
  const now = Date.now();
  const cached = tokenCache[store.domain];
  if (cached && cached.expires > now + 60000) return cached.token;
  const url = `https://${store.domain}/admin/oauth/access_token`;
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      client_id: store.clientId,
      client_secret: store.clientSecret,
      grant_type: 'client_credentials',
    }),
  });
  const data = await resp.json();
  if (!resp.ok || !data.access_token) {
    throw new Error('token-fout: ' + JSON.stringify(data));
  }
  const expiresMs = (data.expires_in ? data.expires_in : 86399) * 1000;
  tokenCache[store.domain] = { token: data.access_token, expires: now + expiresMs };
  return data.access_token;
}

function lastSunday(year, month) {
  const d = new Date(Date.UTC(year, month + 1, 0, 1, 0, 0));
  const day = d.getUTCDay();
  d.setUTCDate(d.getUTCDate() - day);
  return d.getTime();
}
function amsOffsetHours(d) {
  const year = d.getUTCFullYear();
  const dstStart = lastSunday(year, 2);
  const dstEnd = lastSunday(year, 9);
  const t = d.getTime();
  return (t >= dstStart && t < dstEnd) ? 2 : 1;
}
function amsMidnightUTC(offsetDays) {
  const now = new Date();
  const off = amsOffsetHours(now);
  const local = new Date(now.getTime() + off * 3600e3);
  const localMidnight = Date.UTC(
    local.getUTCFullYear(), local.getUTCMonth(), local.getUTCDate() - offsetDays, 0, 0, 0
  );
  return new Date(localMidnight - off * 3600e3);
}

function dateRange(period, from, to) {
  const now = new Date();
  let start, end;
  switch (period) {
    case 'Vandaag':
      start = amsMidnightUTC(0); end = now; break;
    case 'Gisteren':
      start = amsMidnightUTC(1); end = amsMidnightUTC(0); break;
    case 'Deze week': {
      const off = amsOffsetHours(now);
      const local = new Date(now.getTime() + off * 3600e3);
      const day = (local.getUTCDay() + 6) % 7;
      start = amsMidnightUTC(day); end = now; break;
    }
    case 'Deze maand': {
      const off = amsOffsetHours(now);
      const local = new Date(now.getTime() + off * 3600e3);
      const dayOfMonth = local.getUTCDate() - 1;
      start = amsMidnightUTC(dayOfMonth); end = now; break;
    }
    case '30 dagen':
      start = amsMidnightUTC(30); end = now; break;
    case 'Custom':
      start = from ? new Date(from + 'T00:00:00+02:00') : amsMidnightUTC(6);
      end = to ? new Date(to + 'T23:59:59+02:00') : now; break;
    default:
      start = amsMidnightUTC(0); end = now;
  }
  return { start: start.toISOString(), end: end.toISOString() };
}

async function fetchStore(store, startISO, endISO) {
  if (!store.domain || !store.clientId || !store.clientSecret) {
    return { rev: 0, refunds: 0, orders: 0, error: 'store niet geconfigureerd' };
  }
  let token;
  try {
    token = await getAccessToken(store);
  } catch (e) {
    return { rev: 0, refunds: 0, orders: 0, error: String(e.message || e) };
  }
  const url = `https://${store.domain}/admin/api/${API_VERSION}/graphql.json`;
  const searchQuery = `created_at:>=${startISO} created_at:<=${endISO}`;
  let rev = 0, refunds = 0, orderCount = 0;
  let cursor = null, hasNext = true;
  while (hasNext) {
    const query = `
      query($cursor: String) {
        orders(first: 100, after: $cursor, query: ${JSON.stringify(searchQuery)}) {
          edges {
            cursor
            node {
              totalPriceSet { shopMoney { amount } }
              refunds { totalRefundedSet { shopMoney { amount } } }
            }
          }
          pageInfo { hasNextPage endCursor }
        }
      }`;
    const resp = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Shopify-Access-Token': token,
      },
      body: JSON.stringify({ query, variables: { cursor } }),
    });
    const json = await resp.json();
    if (json.errors) {
      return { rev: 0, refunds: 0, orders: 0, error: JSON.stringify(json.errors) };
    }
    const conn = json.data.orders;
    for (const edge of conn.edges) {
      const n = edge.node;
      rev += parseFloat(n.totalPriceSet?.shopMoney?.amount || '0');
      orderCount += 1;
      for (const r of (n.refunds || [])) {
        refunds += parseFloat(r.totalRefundedSet?.shopMoney?.amount || '0');
      }
    }
    hasNext = conn.pageInfo.hasNextPage;
    cursor = conn.pageInfo.endCursor;
  }
  return { rev, refunds, orders: orderCount };
}

function toMetrics(rev, refunds, orders, adSpend) {
  const cogs = rev * COGS_RATE;
  const netRev = rev - refunds;
  const profit = netRev - adSpend - cogs;
  const roas = adSpend > 0 ? rev / adSpend : 0;
  const aov = orders > 0 ? rev / orders : 0;
  const margin = netRev > 0 ? (profit / netRev) * 100 : 0;
  return {
    rev: Math.round(rev), refunds: Math.round(refunds), cogs: Math.round(cogs),
    spend: Math.round(adSpend), orders, netRev: Math.round(netRev),
    profit: Math.round(profit), roas: +roas.toFixed(2), aov: +aov.toFixed(1),
    margin: +margin.toFixed(1),
  };
}

export default async function handler(req, res) {
  try {
    const period = req.query.period || 'Vandaag';
    const from = req.query.from || null;
    const to = req.query.to || null;
    const { start, end } = dateRange(period, from, to);
    const adSpend = { fr: 0, uk: 0 };
    const [fr, uk] = await Promise.all([
      fetchStore(STORES.fr, start, end),
      fetchStore(STORES.uk, start, end),
    ]);
    const ukEur = {
      rev: uk.rev * GBP_TO_EUR,
      refunds: uk.refunds * GBP_TO_EUR,
      orders: uk.orders,
    };
    const result = {
      period,
      range: { start, end },
      fr: { ...toMetrics(fr.rev, fr.refunds, fr.orders, adSpend.fr), currency: 'EUR' },
      uk: { ...toMetrics(uk.rev, uk.refunds, uk.orders, adSpend.uk), currency: 'GBP' },
      all: { ...toMetrics(fr.rev + ukEur.rev, fr.refunds + ukEur.refunds, fr.orders + ukEur.orders, adSpend.fr + adSpend.uk), currency: 'EUR' },
      errors: { fr: fr.error || null, uk: uk.error || null },
    };
    res.setHeader('Cache-Control', 's-maxage=60');
    res.status(200).json(result);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
}
