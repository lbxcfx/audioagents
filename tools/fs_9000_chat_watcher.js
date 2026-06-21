const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { spawnSync } = require('child_process');

const docker = process.env.AICALL_DOCKER || '/home/lbx/.local/bin/docker';
const dockerConfig = process.env.AICALL_DOCKER_CONFIG || '/tmp/aicall-docker-config';
const container = process.env.AICALL_FS_CONTAINER || 'aicall-freeswitch';
const adminBase = process.env.AICALL_ADMIN_BASE || 'http://127.0.0.1:8080';
const winTmp = process.env.AICALL_WIN_TMP || 'F:\\tmp';
const wslTmp = process.env.AICALL_WSL_TMP || '/mnt/f/tmp';
const pollMs = Number(process.env.AICALL_WATCH_POLL_MS || 500);
const vadThreshold = Number(process.env.AICALL_VAD_RMS || 280);
const vadMode = (process.env.AICALL_VAD_MODE || 'hybrid').toLowerCase();
const vadUrl = process.env.AICALL_VAD_URL || '';
const vadProbThreshold = Number(process.env.AICALL_VAD_PROB || 0.5);
const vadEndSilenceMs = Number(process.env.AICALL_VAD_END_SILENCE_MS || 900);
const vadFrameMs = Number(process.env.AICALL_VAD_FRAME_MS || 200);
const maxListenMs = Number(process.env.AICALL_STREAM_MAX_MS || 30000);
const noGrowthEndMs = Number(process.env.AICALL_STREAM_NO_GROWTH_END_MS || 1200);
const processed = new Set();
let busy = false;

function nowIso() { return new Date().toISOString(); }
function sleep(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

function readEnvKey() {
  if (process.env.DASHSCOPE_API_KEY) return process.env.DASHSCOPE_API_KEY.trim();
  for (const file of [path.join(process.cwd(), '.env'), path.join(__dirname, '..', '.env')]) {
    try {
      const text = fs.readFileSync(file, 'utf8');
      for (const line of text.split(/\r?\n/)) {
        const m = line.match(/^DASHSCOPE_API_KEY=(.*)$/);
        if (m && m[1].trim()) return m[1].trim();
      }
    } catch (_) {}
  }
  throw new Error('Missing DASHSCOPE_API_KEY in environment or .env');
}

function wslDocker(args, input, binaryOutput) {
  const script = [docker, '--config', dockerConfig, ...args]
    .map((x) => `'${String(x).replace(/'/g, `'\\''`)}'`)
    .join(' ');
  const r = spawnSync('wsl', ['bash', '-lc', script], {
    input,
    encoding: binaryOutput || input ? undefined : 'utf8',
    maxBuffer: 60 * 1024 * 1024,
  });
  if (r.status !== 0) throw new Error((r.stderr || r.stdout || '').toString());
  return r.stdout;
}

function listRequests() {
  const out = wslDocker(['exec', container, 'sh', '-lc', 'ls /tmp/aicall_9000_*_request.wav 2>/dev/null || true']);
  return out.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
}

function remoteBytes(remote) {
  return wslDocker(['exec', container, 'sh', '-lc', `cat '${remote}' 2>/dev/null || true`], undefined, true);
}

function copyToContainer(local, remote) {
  const bytes = fs.readFileSync(local);
  wslDocker(['exec', '-i', container, 'sh', '-lc', `cat > '${remote}'`], bytes);
}

function downloadWithWindowsCurl(url, local) {
  const r = spawnSync('curl.exe', ['-L', '--fail', '--max-time', '120', '-o', local, url], {
    encoding: 'utf8',
    maxBuffer: 10 * 1024 * 1024,
  });
  if (r.status !== 0) throw new Error(r.stderr || r.stdout || 'windows curl download failed');
}

function rms16le(buf) {
  const n = Math.floor(buf.length / 2);
  if (!n) return 0;
  let sum = 0;
  for (let i = 0; i < n; i++) {
    const v = buf.readInt16LE(i * 2);
    sum += v * v;
  }
  return Math.sqrt(sum / n);
}

function parseWavHeader(buf) {
  if (buf.length < 44 || buf.toString('ascii', 0, 4) !== 'RIFF' || buf.toString('ascii', 8, 12) !== 'WAVE') return null;
  let channels = 0;
  let sampleRate = 0;
  let bits = 0;
  let dataOffset = 0;
  for (let p = 12; p + 8 <= buf.length;) {
    const id = buf.toString('ascii', p, p + 4);
    const size = buf.readUInt32LE(p + 4);
    const body = p + 8;
    if (id === 'fmt ' && body + 16 <= buf.length) {
      channels = buf.readUInt16LE(body + 2);
      sampleRate = buf.readUInt32LE(body + 4);
      bits = buf.readUInt16LE(body + 14);
    } else if (id === 'data') {
      dataOffset = body;
      break;
    }
    p = body + size + (size % 2);
  }
  if (!channels || !sampleRate || bits !== 16 || !dataOffset) return null;
  return { channels, sampleRate, bits, dataOffset };
}

async function vadByHttp(pcm, sampleRate) {
  if (!vadUrl || typeof fetch !== 'function') return null;
  const sep = vadUrl.includes('?') ? '&' : '?';
  const response = await fetch(`${vadUrl}${sep}sample_rate=${sampleRate}`, {
    method: 'POST',
    body: pcm,
    signal: AbortSignal.timeout(Number(process.env.AICALL_VAD_TIMEOUT_MS || 500)),
  });
  if (!response.ok) throw new Error(`VAD HTTP ${response.status}`);
  const json = await response.json();
  return { speech: Boolean(json.speech), probability: Number(json.probability || 0) };
}

async function detectSpeech(chunk, sampleRate) {
  const energyRms = rms16le(chunk);
  const energySpeech = energyRms >= vadThreshold;
  let silero = null;
  if (vadUrl && vadMode !== 'energy') {
    try {
      silero = await vadByHttp(chunk, sampleRate);
    } catch (e) {
      console.error(`[watcher] vad http failed: ${e.message}`);
    }
  }
  const sileroSpeech = silero ? (silero.speech || silero.probability >= vadProbThreshold) : false;
  let speech;
  if (vadMode === 'silero') speech = silero ? sileroSpeech : energySpeech;
  else if (vadMode === 'energy') speech = energySpeech;
  else speech = energySpeech || sileroSpeech;
  return { speech, rms: energyRms, probability: silero ? silero.probability : null };
}

function postReply(userText, replyWslPath) {
  const body = new URLSearchParams();
  body.set('userText', userText || '');
  body.set('replyAudioPath', replyWslPath);
  const r = spawnSync('curl.exe', [
    '-s', '-m', '80', '-X', 'POST', `${adminBase}/api/ai/diag/phoneChatReply`,
    '-H', 'Content-Type: application/x-www-form-urlencoded', '--data', body.toString(),
  ], { encoding: 'utf8', maxBuffer: 10 * 1024 * 1024 });
  if (r.status !== 0) throw new Error(r.stderr || r.stdout || 'curl failed');
  const json = JSON.parse(r.stdout);
  if (json.code !== 0) throw new Error(JSON.stringify(json));
  return json.data;
}

function findText(node) {
  if (!node || typeof node !== 'object') return null;
  if (typeof node.text === 'string') return node.text;
  for (const k of Object.keys(node)) {
    const v = findText(node[k]);
    if (v != null) return v;
  }
  return null;
}

async function streamAsrFromGrowingWav(remoteReq, localReq) {
  if (typeof WebSocket !== 'function') {
    throw new Error('Node.js WebSocket is unavailable; use Node 22+ or provide a WebSocket implementation');
  }
  const apiKey = readEnvKey();
  const taskId = crypto.randomUUID();
  const timeline = { startAt: Date.now(), remoteReq };
  let firstResultAt = 0;
  let finalText = '';
  let lastText = '';
  let error = '';
  let taskStartedResolve;
  let taskStartedReject;
  let taskFinishedResolve;
  let wsOpenResolve;
  let wsOpenReject;
  const taskStarted = new Promise((resolve, reject) => { taskStartedResolve = resolve; taskStartedReject = reject; });
  const taskFinished = new Promise((resolve) => { taskFinishedResolve = resolve; });
  const wsOpen = new Promise((resolve, reject) => { wsOpenResolve = resolve; wsOpenReject = reject; });

  const ws = new WebSocket('wss://dashscope.aliyuncs.com/api-ws/v1/inference/', {
    headers: { Authorization: `Bearer ${apiKey}`, 'X-DashScope-DataInspection': 'enable' },
  });

  ws.addEventListener('open', () => {
    timeline.wsOpenMs = Date.now() - timeline.startAt;
    wsOpenResolve();
    ws.send(JSON.stringify({
      header: { action: 'run-task', task_id: taskId, streaming: 'duplex' },
      payload: {
        task_group: 'audio',
        task: 'asr',
        function: 'recognition',
        model: process.env.DASHSCOPE_REALTIME_ASR_MODEL || 'paraformer-realtime-v2',
        parameters: {
          format: 'pcm',
          sample_rate: Number(process.env.AICALL_ASR_SAMPLE_RATE || 8000),
          language_hints: ['zh'],
          disfluency_removal_enabled: false,
        },
        input: {},
      },
    }));
  });

  ws.addEventListener('message', (ev) => {
    try {
      const msg = JSON.parse(String(ev.data));
      const event = msg.header && msg.header.event;
      const text = findText(msg) || '';
      if (event === 'task-started') {
        timeline.taskStartedMs = Date.now() - timeline.startAt;
        taskStartedResolve();
      } else if (event === 'result-generated') {
        if (text) {
          if (!firstResultAt) {
            firstResultAt = Date.now();
            timeline.asrFirstResultMs = firstResultAt - timeline.startAt;
          }
          lastText = text.trim();
          const sentence = msg.payload && msg.payload.output && msg.payload.output.sentence;
          if (sentence && sentence.sentence_end) finalText = lastText;
        }
      } else if (event === 'task-finished') {
        timeline.asrFinishedMs = Date.now() - timeline.startAt;
        taskFinishedResolve();
      } else if (event === 'task-failed') {
        error = JSON.stringify(msg);
        taskStartedReject(new Error(error));
        taskFinishedResolve();
      }
    } catch (e) {
      error = e.message;
    }
  });

  ws.addEventListener('error', (e) => {
    error = e.message || 'websocket error';
    wsOpenReject(new Error(error));
    taskStartedReject(new Error(error));
    taskFinishedResolve();
  });
  ws.addEventListener('close', () => taskFinishedResolve());

  await Promise.race([wsOpen, sleep(10000).then(() => { throw new Error('ASR websocket open timeout'); })]);
  await Promise.race([taskStarted, sleep(10000).then(() => { throw new Error('ASR task-started timeout'); })]);

  let header = null;
  let offset = 0;
  let speechStarted = false;
  let silenceMs = 0;
  let speechMs = 0;
  let lastGrowthAt = Date.now();
  let sentBytes = 0;
  let frames = 0;
  let vadProbeBytes = 0;
  let pending = Buffer.alloc(0);
  let lastVad = { speech: false, rms: 0, probability: null };

  while (Date.now() - timeline.startAt < maxListenMs) {
    const bytes = remoteBytes(remoteReq);
    if (bytes.length > offset) lastGrowthAt = Date.now();
    if (!header) {
      header = parseWavHeader(bytes);
      if (!header) {
        await sleep(100);
        continue;
      }
      timeline.wavReadyMs = Date.now() - timeline.startAt;
      timeline.sampleRate = header.sampleRate;
      timeline.channels = header.channels;
      timeline.bits = header.bits;
      offset = header.dataOffset;
    }

    if (bytes.length > offset) {
      let chunk = bytes.subarray(offset);
      offset = bytes.length;
      if (chunk.length % 2) chunk = chunk.subarray(0, chunk.length - 1);
      if (chunk.length > 0) {
        const chunkMs = chunk.length / 2 / Math.max(1, header.sampleRate * header.channels) * 1000;
        pending = pending.length ? Buffer.concat([pending, chunk]) : chunk;
        const vadFrameBytes = Math.max(320, Math.floor(header.sampleRate * header.channels * 2 * vadFrameMs / 1000));
        if (pending.length >= vadFrameBytes) {
          const vadFrame = pending.subarray(0, pending.length - (pending.length % 2));
          pending = Buffer.alloc(0);
          lastVad = await detectSpeech(vadFrame, header.sampleRate);
          vadProbeBytes += vadFrame.length;
        } else {
          const rms = rms16le(chunk);
          lastVad = { speech: rms >= vadThreshold, rms, probability: lastVad.probability };
        }
        if (!speechStarted && lastVad.speech) {
          speechStarted = true;
          timeline.vadStartMs = Date.now() - timeline.startAt;
          timeline.vadStartRms = Number(lastVad.rms.toFixed(1));
          if (lastVad.probability != null) timeline.vadStartProbability = Number(lastVad.probability.toFixed(4));
          console.log(`[watcher] vad start ${remoteReq}; rms=${lastVad.rms.toFixed(1)} prob=${lastVad.probability == null ? '-' : lastVad.probability.toFixed(4)} mode=${vadMode}`);
        }
        if (speechStarted) {
          speechMs += chunkMs;
          if (!lastVad.speech) silenceMs += chunkMs;
          else silenceMs = 0;
          ws.send(chunk);
          sentBytes += chunk.length;
          frames++;
          if (silenceMs >= vadEndSilenceMs && speechMs >= 300) {
            timeline.vadEndMs = Date.now() - timeline.startAt;
            console.log(`[watcher] vad end ${remoteReq}; silenceMs=${silenceMs.toFixed(0)} sentBytes=${sentBytes}`);
            break;
          }
        }
      }
    }

    if (speechStarted && Date.now() - lastGrowthAt > noGrowthEndMs) {
      timeline.vadEndMs = Date.now() - timeline.startAt;
      console.log(`[watcher] vad/file end ${remoteReq}; sentBytes=${sentBytes}`);
      break;
    }
    await sleep(100);
  }

  fs.writeFileSync(localReq, remoteBytes(remoteReq));
  timeline.audioSentBytes = sentBytes;
  timeline.audioSentFrames = frames;
  timeline.vadProbeBytes = vadProbeBytes;
  if (!speechStarted) {
    timeline.vadEndMs = Date.now() - timeline.startAt;
    console.log(`[watcher] vad timeout/no speech ${remoteReq}; lastRms=${lastVad.rms.toFixed(1)} sentBytes=${sentBytes}`);
  }
  if (ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ header: { action: 'finish-task', task_id: taskId, streaming: 'duplex' }, payload: { input: {} } }));
  }
  await Promise.race([taskFinished, sleep(8000)]);
  try { ws.close(); } catch (_) {}
  timeline.totalAsrMs = Date.now() - timeline.startAt;
  return { text: finalText || lastText, error, timeline, sentBytes, frames, taskId };
}

async function handleRequest(remoteReq) {
  const base = path.posix.basename(remoteReq).replace('_request.wav', '');
  const localReq = path.join(winTmp, `${base}_request.wav`);
  const localReply = path.join(winTmp, `${base}_reply.wav`);
  const wslReply = `${wslTmp}/${base}_reply.wav`;
  const remoteReply = `/tmp/${base}_reply.wav`;
  const startAt = Date.now();
  console.log(`[timeline] ${base} seen at ${nowIso()}`);
  const asr = await streamAsrFromGrowingWav(remoteReq, localReq);
  const asrDoneAt = Date.now();
  console.log(`[timeline] ${base} asr done ms=${asrDoneAt - startAt}; text="${asr.text}"; error="${asr.error || ''}"; asr=${JSON.stringify(asr.timeline)}`);
  const replyData = postReply(asr.text || '', wslReply);
  const replyAt = Date.now();
  console.log(`[timeline] ${base} llm+tts done ms=${replyAt - startAt}; backend=${JSON.stringify(replyData.timeline || {})}`);
  if (!fs.existsSync(localReply) && replyData.ttsAudioUrl) {
    downloadWithWindowsCurl(replyData.ttsAudioUrl, localReply);
  }
  const downloadAt = Date.now();
  copyToContainer(localReply, remoteReply);
  const doneAt = Date.now();
  console.log(`[timeline] ${base} reply copied ms=${doneAt - startAt}; downloadMs=${downloadAt - replyAt}; copyBackMs=${doneAt - downloadAt}; remoteReply=${remoteReply}; answer="${replyData.answer || ''}"`);
}

async function tick() {
  if (busy) return;
  const requests = listRequests();
  const next = requests.find((r) => !processed.has(r));
  if (!next) return;
  processed.add(next);
  busy = true;
  try {
    await handleRequest(next);
  } catch (e) {
    console.error(`[watcher] failed ${next}: ${e.stack || e.message}`);
  } finally {
    busy = false;
  }
}

console.log(`[watcher] fs_9000_chat_watcher streaming started admin=${adminBase} vadMode=${vadMode} vadUrl=${vadUrl || '-'} vadRms=${vadThreshold} vadProb=${vadProbThreshold} vadEndSilenceMs=${vadEndSilenceMs}`);
for (const existing of listRequests()) processed.add(existing);
setInterval(() => tick().catch((e) => console.error(e.stack || e.message)), pollMs);
