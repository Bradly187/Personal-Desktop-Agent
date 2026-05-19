/**
 * Polly Bidirectional Streaming TTS Sidecar
 *
 * Bridges Python (which boto3 cannot use for StartSpeechSynthesisStream) to the
 * AWS SDK for JavaScript v3, which has full support for the bidirectional HTTP/2
 * streaming API.
 *
 * Protocol:
 *   POST /speak   { text, voice?, engine?, language_code? }
 *                 → 200 audio/ogg  (complete OGG Vorbis body)
 *                 → 500 { error }
 *
 *   GET  /health  → 200 { status: "ok", voice, engine }
 *
 * Usage:
 *   node server.js            # default port 8766
 *   TTS_PORT=9000 node server.js
 *
 * AWS credentials are read from the standard chain:
 *   ~/.aws/credentials  →  env vars  →  IAM role
 * Region defaults to us-east-1 (Bidirectional Streaming is available there).
 */

import express from "express";
import { Readable } from "stream";
import {
  PollyClient,
  StartSpeechSynthesisStreamCommand,
} from "@aws-sdk/client-polly";
import { fromIni, fromEnv } from "@aws-sdk/credential-providers";

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const PORT          = parseInt(process.env.TTS_PORT      || "8766", 10);
const REGION        = process.env.AWS_DEFAULT_REGION     || "us-east-1";
const DEFAULT_VOICE = process.env.TTS_VOICE              || "Danielle";
const DEFAULT_ENGINE= process.env.TTS_ENGINE             || "generative";

// ---------------------------------------------------------------------------
// Polly client — credentials from ~/.aws/credentials then env vars
// ---------------------------------------------------------------------------

let credentials;
try {
  credentials = fromIni();  // reads ~/.aws/credentials [default] profile
} catch {
  credentials = fromEnv();  // fallback to AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
}

const polly = new PollyClient({ region: REGION, credentials });

// ---------------------------------------------------------------------------
// Core: open a StartSpeechSynthesisStream session, send all text, return audio
// ---------------------------------------------------------------------------

/**
 * Synthesize `text` using Polly's Bidirectional Streaming API.
 *
 * Returns a Buffer containing the complete OGG Vorbis audio, or throws on error.
 *
 * The bidirectional stream works as follows:
 *   1. We provide an async generator as the input event stream.
 *      It yields TextEvent(s) followed by CloseStreamEvent.
 *   2. Polly returns an AudioStream async iterable.
 *      We consume AudioEvent chunks until StreamClosedEvent.
 *
 * @param {string}  text         - Text to synthesize (already sentence-chunked by caller)
 * @param {string}  voice        - Polly voice ID (default Danielle)
 * @param {string}  engine       - "generative" | "neural" | "standard"
 * @param {string}  languageCode - BCP-47 language tag (default en-US)
 * @returns {Promise<Buffer>}    - Complete OGG Vorbis audio bytes
 */
async function synthesize(
  text,
  voice       = DEFAULT_VOICE,
  engine      = DEFAULT_ENGINE,
  languageCode = "en-US"
) {
  // Input event stream: one TextEvent then CloseStreamEvent.
  // Wrapped with Readable.from() because the SDK v3 event stream middleware
  // expects a Node.js Readable stream, not a bare async generator.
  const actionStream = Readable.from(
    (async function* () {
      yield { TextEvent: { Text: text } };
      yield { CloseStreamEvent: {} };
    })()
  );

  const command = new StartSpeechSynthesisStreamCommand({
    Engine:       engine,
    LanguageCode: languageCode,
    VoiceId:      voice,
    OutputFormat: "ogg_vorbis",   // OGG: works with Generative; decodable by soundfile in Python
    ActionStream: actionStream,   // SDK v3 field name (confirmed via schema_0.js inspection)
  });

  const response = await polly.send(command);

  // Output field is EventStream (not AudioStream — confirmed via dist-types/models_0.d.ts).
  // Each event is a StartSpeechSynthesisStreamEventStream union member:
  //   AudioEventMember      → event.AudioEvent.AudioChunk (Uint8Array)
  //   StreamClosedEventMember → signals end of stream
  const chunks = [];
  for await (const event of response.EventStream) {
    if (event.AudioEvent?.AudioChunk) {
      chunks.push(Buffer.from(event.AudioEvent.AudioChunk));
    }
    // StreamClosedEvent → for-of ends naturally when the async iterable closes
  }

  if (chunks.length === 0) {
    throw new Error("Polly returned no audio chunks");
  }

  return Buffer.concat(chunks);
}

// ---------------------------------------------------------------------------
// HTTP server
// ---------------------------------------------------------------------------

const app = express();
app.use(express.json({ limit: "1mb" }));

/**
 * POST /speak
 * Body: { text: string, voice?: string, engine?: string, language_code?: string }
 * Returns: audio/ogg binary body on success, JSON error on failure.
 *
 * Python side decodes the OGG with soundfile and plays via sounddevice.
 */
app.post("/speak", async (req, res) => {
  const {
    text,
    voice        = DEFAULT_VOICE,
    engine       = DEFAULT_ENGINE,
    language_code = "en-US",
  } = req.body;

  if (!text || !text.trim()) {
    return res.status(400).json({ error: "text is required and must be non-empty" });
  }

  try {
    const audioBuffer = await synthesize(text.trim(), voice, engine, language_code);
    res.set("Content-Type", "audio/ogg");
    res.set("X-Audio-Chars", String(text.length));
    res.send(audioBuffer);
  } catch (err) {
    console.error("[TTS] Polly error:", err.message);
    res.status(500).json({ error: err.message, code: err.name });
  }
});

/**
 * GET /health
 * Returns service status. Python uses this to confirm the sidecar is up.
 */
app.get("/health", (_req, res) => {
  res.json({
    status:  "ok",
    voice:   DEFAULT_VOICE,
    engine:  DEFAULT_ENGINE,
    region:  REGION,
  });
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

app.listen(PORT, "127.0.0.1", () => {
  console.log(`[TTS] Polly sidecar listening on http://127.0.0.1:${PORT}`);
  console.log(`[TTS] Engine=${DEFAULT_ENGINE}  Voice=${DEFAULT_VOICE}  Region=${REGION}`);
});
