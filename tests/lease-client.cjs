// Disposable Redis-time heartbeat and lease fixture using ioredis 6.0.0.
const Redis = require('/client/node_modules/ioredis');
const fs = require('node:fs');
const { performance } = require('node:perf_hooks');

const heartbeat = `
local time = redis.call('time')
local now = tonumber(time[1]) * 1000 + math.floor(tonumber(time[2]) / 1000)
if ARGV[2] == 'renew' then
    local score = redis.call('zscore', KEYS[1], ARGV[1])
    if not score or tonumber(score) <= now - tonumber(ARGV[3]) then return 0 end
end
if ARGV[2] == 'create' then
    redis.call('zadd', KEYS[1], 'NX', now, ARGV[1])
else
    redis.call('zadd', KEYS[1], now, ARGV[1])
end
return now
`;
const stats = {
  renewals: 0,
  lost: 0,
  errors: 0,
  maxRenewalMs: 0,
  leaseLosses: [],
  commandErrors: [],
  acknowledgedWrites: 0,
};
const clients = [];
const timers = [];
let stopping = false;

function writeStats() {
  fs.writeFileSync('/results/stats-next.json', JSON.stringify(stats));
  fs.renameSync('/results/stats-next.json', '/results/stats.json');
}

function connection() {
  const client = new Redis({
    sentinels: [{ host: 'redis-sentinel', port: 26379 }],
    name: 'redis',
    username: 'fixture-client',
    password: 'fixture-client-password',
    sentinelUsername: 'fixture-client',
    sentinelPassword: 'fixture-client-password',
    connectTimeout: 1000,
    sentinelCommandTimeout: 1000,
    updateSentinels: false,
    failoverDetector: true,
    sentinelMaxConnections: 1,
    reconnectOnError: (error) => (error.message.startsWith('READONLY') ? 2 : false),
  });
  client.on('error', () => {
    stats.errors++;
  });
  clients.push(client);
  return client;
}

// A bounded fixture journal catches acknowledged writes lost during a handoff.
// Idempotent keys make retry after an ambiguous connection failure safe.
async function writeJournal() {
  const client = connection();
  while (!stopping) {
    const sequence = stats.acknowledgedWrites;
    try {
      await client.set(`lease:ack:${sequence}`, String(sequence));
      stats.acknowledgedWrites++;
    } catch (error) {
      if (!stopping) {
        stats.errors++;
        if (stats.commandErrors.length < 10) stats.commandErrors.push(error.message);
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
}

async function member(index) {
  const client = connection();
  const ttl = 15000;
  let safeUntil = 0;
  let inFlight = false;
  let fenced = false;
  async function renew(create = false) {
    if (inFlight || stopping) return;
    inFlight = true;
    const started = performance.now();
    try {
      const result = await client.eval(
        heartbeat,
        1,
        'lease:members',
        String(index),
        create ? 'create' : 'renew',
        ttl,
      );
      stats.maxRenewalMs = Math.max(stats.maxRenewalMs, performance.now() - started);
      if (!result) {
        if (!fenced) {
          stats.lost++;
          stats.leaseLosses.push({
            member: index,
            reason: 'Redis rejected renewal',
            at: new Date().toISOString(),
          });
        }
        fenced = true;
      } else {
        safeUntil = started + ttl;
        stats.renewals++;
      }
    } catch (error) {
      stats.errors++;
      if (stats.commandErrors.length < 10) stats.commandErrors.push(error.message);
    } finally {
      inFlight = false;
    }
  }
  await renew(true);
  const safety = setInterval(() => {
    if (!fenced && performance.now() >= safeUntil) {
      stats.lost++;
      stats.leaseLosses.push({
        member: index,
        reason: 'Safety timer expired',
        at: new Date().toISOString(),
      });
      fenced = true;
    }
  }, 10);
  timers.push(safety);
  await new Promise((resolve) => setTimeout(resolve, index * 1000));
  timers.push(
    setInterval(() => {
      void renew();
    }, 5000),
  );
}

async function main() {
  const writes = writeJournal();
  await Promise.all([0, 1, 2, 3].map(member));
  fs.writeFileSync('/results/ready', 'ready');
  while (!fs.existsSync('/results/stop')) {
    writeStats();
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  stopping = true;
  timers.forEach(clearInterval);
  clients.forEach((client) => client.disconnect());
  await writes;
  writeStats();
}
main().catch((error) => {
  console.error(error.message);
  process.exit(1);
});
