// Vercel serverless functie: haalt orders + refunds op uit Shopify (FR + UK)
// Nieuwe Shopify-flow (2026): Client ID + Secret -> access token (client credentials grant).
// Credentials komen uit Vercel Environment Variables.

const API_VERSION = '2026-01';
const COGS_RATE = 0.29; // COGS = 29% van omzet

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

const tokenCache = {}; // domain -> { token, expires }

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

function dateRange(period, from, to) {
  const now = new Date();
  const startOfDay = d => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  let start, end;
  const today = startOfDay(now);
  switch (period) {
    case 'Vandaag': start = today; end = now; break;
    case 'Gisteren': start = new Date(today.getTime() - 864e5); end = today; break;
    case 'Deze week': {
      const day = (now.getDay() + 6) % 7;
      start = new Date(today.getTime() - day * 864e5); end = now; break;
    }
    case 'Deze maand': start = new Date(now.getFullYear(), now.getMonth(), 1); end = now; break;
    case '30 dagen': start = new Date(today.getTime() - 30 * 864e5); end = now; break;
    case 'Custom':
      start = from ? new Date(from) : new Date(today.getTime() - 6 * 864e5);
      end = to ? new Date(new Date(to).getTime() + 864e5) : now; break;
    default: start = today; end = now;
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

    const adSpend = { fr: 0, uk: 0 }; // Google Ads komt later

    const [fr, uk] = await Promise.all([
      fetchStore(STORES.fr, start, end),
      fetchStore(STORES.uk, start, end),
    ]);

    const result = {
      period,
      range: { start, end },
      fr: toMetrics(fr.rev, fr.refunds, fr.orders, adSpend.fr),
      uk: toMetrics(uk.rev, uk.refunds, uk.orders, adSpend.uk),
      all: toMetrics(fr.rev + uk.rev, fr.refunds + uk.refunds, fr.orders + uk.orders, adSpend.fr + adSpend.uk),
      errors: { fr: fr.error || null, uk: uk.error || null },
    };

    res.setHeader('Cache-Control', 's-maxage=60');
    res.status(200).json(result);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
}
