const dgram = require('dgram');

const listenAddress = process.env.RTP_RELAY_LISTEN || '0.0.0.0';
const targetAddress = process.env.RTP_RELAY_TARGET || '2.0.0.1';
const startPort = Number(process.env.RTP_RELAY_START || 16384);
const endPort = Number(process.env.RTP_RELAY_END || 16484);

const clients = new Map();
let bound = 0;

for (let port = startPort; port <= endPort; port++) {
  const sock = dgram.createSocket('udp4');
  const key = String(port);

  sock.on('message', (msg, rinfo) => {
    const fromTarget = rinfo.address === targetAddress;
    if (fromTarget) {
      const client = clients.get(key);
      if (client) sock.send(msg, client.port, client.address);
      return;
    }
    clients.set(key, { address: rinfo.address, port: rinfo.port, at: Date.now() });
    sock.send(msg, port, targetAddress);
  });

  sock.on('error', (err) => {
    console.error(`[${port}] ${err.message}`);
  });

  sock.bind(port, listenAddress, () => {
    bound++;
    if (bound === 1 || bound === (endPort - startPort + 1)) {
      console.log(`RTP relay bound ${bound}/${endPort - startPort + 1}, ${listenAddress}:${startPort}-${endPort} -> ${targetAddress}:same-port`);
    }
  });
}

setInterval(() => {
  const now = Date.now();
  let active = 0;
  for (const [port, client] of clients.entries()) {
    if (now - client.at < 30000) active++;
  }
  console.log(`RTP relay heartbeat bound=${bound} activePorts=${active}`);
}, 10000);
