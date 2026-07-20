// Vercel serverless functie: haalt orders + refunds op uit Shopify (FR + UK)
// en rekent de dashboard-cijfers uit. Tokens komen uit Vercel Environment Variables.

const API_VERSION = '2026-01';
const COGS_RATE = 0.29; // COGS = 29% van omzet

// Winkelconfig: domein per store, token uit environment variables
const STORES = {
  fr: { domain: process.env.FR_SHOP_DOMAIN, token: process.env.FR_SHOP_TOKEN },
  uk: { domain: process.env.UK_SHOP_DOMAIN, token: process.env.UK_SHOP_TOKEN },
};

// Bepaal datumbereik (ISO) op basis van de gekozen periode
function dateRange(period, from, to) {
  const now = new Date();
  const startOfDay = d => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  let start, end;
  const today = startOfDay(now);
  switch (period) {
    case 'Vandaag':
      start = today; end = now; break;
    case 'Gisteren':
      start = new Date(today.getTime() - 864e5); end = today; break;
    case 'Deze week': {
      const day = (now.getDay() + 6) % 7; // maandag = 0
      start = new Date(today.getTime() - day * 864e5); end = now; break;
    }
    case 'Deze maand':
      start = new Date(now.getFullYear(), now.getMonth(), 1); end = now; break;
    case '30 dagen':
      start = new Date(today.getTime() - 30 * 864e5); end = now; break;
    case 'Custom':
      start = from ? new Date(from) : new Date(today.getTime() - 6 * 864e5);
      end = to ? new Date(new Date(to).getTime() + 864e5) : now; break;
    default:
      start = today; end = now;
  }
  return { start: start.toISOString(), end: end.toISOString() };
}

// Haal alle orders (met refunds) op voor een winkel binnen een bereik
async function fetchStore(store, startISO, endISO) {
  if (!store.domain || !store.token) {
    return { rev: 0, refunds: 0, orders: 0, error: 'store niet geconfigureerd' };
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
              currentTotalPriceSet { shopMoney { amount } }
              totalReceivedSet { shopMoney { amount } }
              subtotalPriceSet { shopMoney { amount } }
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
        'X-Shopify-Access-Token': store.token,
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
      const gross = parseFloat(n.totalPriceSet?.shopMoney?.amount || '0');
      rev += gross;
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

// Zet ruwe store-cijfers om naar dashboard-metrics
function toMetrics(rev, refunds, orders, adSpend) {
  const cogs = rev * COGS_RATE;
  const netRev = rev - refunds;
  const profit = netRev - adSpend - cogs;
  const roas = adSpend > 0 ? rev / adSpend : 0;
  const aov = orders > 0 ? rev / orders : 0;
  const margin = netRev > 0 ? (profit / netRev) * 100 : 0;
  return {
    rev: Math.round(rev),
    refunds: Math.round(refunds),
    cogs: Math.round(cogs),
    spend: Math.round(adSpend),
    orders,
    netRev: Math.round(netRev),
    profit: Math.round(profit),
    roas: +roas.toFixed(2),
    aov: +aov.toFixed(1),
    margin: +margin.toFixed(1),
  };
}

export default async function handler(req, res) {
  try {
    const period = req.query.period || 'Vandaag';
    const from = req.query.from || null;
    const to = req.query.to || null;
    const { start, end } = dateRange(period, from, to);

    // Ad spend komt later uit Google Ads; nu 0 tot die koppeling er is
    const adSpend = { fr: 0, uk: 0 };

    const [fr, uk] = await Promise.all([
      fetchStore(STORES.fr, start, end),
      fetchStore(STORES.uk, start, end),
    ]);

    const result = {
      period,
      range: { start, end },
      fr: toMetrics(fr.rev, fr.refunds, fr.orders, adSpend.fr),
      uk: toMetrics(uk.rev, uk.refunds, uk.orders, adSpend.uk),
      all: toMetrics(
        fr.rev + uk.rev,
        fr.refunds + uk.refunds,
        fr.orders + uk.orders,
        adSpend.fr + adSpend.uk
      ),
      errors: { fr: fr.error || null, uk: uk.error || null },
    };

    res.setHeader('Cache-Control', 's-maxage=60');
    res.status(200).json(result);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
}

