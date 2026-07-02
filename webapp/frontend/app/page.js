"use client";

import { useEffect, useRef, useState } from "react";
import { PublicClientApplication } from "@azure/msal-browser";

const BACKEND = process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";
const WS_URL = BACKEND.replace(/^http/, "ws") + "/ws";

function abToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

function base64ToFloat32(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  const int16 = new Int16Array(bytes.buffer);
  const float32 = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) float32[i] = int16[i] / 32768;
  return float32;
}

export default function Home() {
  const [status, setStatus] = useState("Loading configuration...");
  const [account, setAccount] = useState(null);
  const [live, setLive] = useState(false);
  const [log, setLog] = useState([]);

  const cfgRef = useRef(null);
  const msalRef = useRef(null);
  const wsRef = useRef(null);
  const ctxRef = useRef(null);
  const streamRef = useRef(null);
  const playbackRef = useRef(null);
  const readyRef = useRef(false);

  const addLog = (line) => setLog((prev) => [...prev.slice(-40), line]);

  // Load backend config + initialize MSAL once.
  useEffect(() => {
    (async () => {
      try {
        const cfg = await fetch(`${BACKEND}/config`).then((r) => r.json());
        cfgRef.current = cfg;
        const pca = new PublicClientApplication({
          auth: {
            clientId: cfg.clientId,
            authority: `https://login.microsoftonline.com/${cfg.tenantId}`,
            redirectUri: window.location.origin,
          },
          cache: { cacheLocation: "sessionStorage" },
        });
        await pca.initialize();
        msalRef.current = pca;
        const existing = pca.getAllAccounts()[0];
        if (existing) setAccount(existing);
        setStatus("Ready. Sign in to continue.");
      } catch (err) {
        setStatus("Could not reach backend /config. Is the backend running?");
      }
    })();
  }, []);

  const signIn = async () => {
    try {
      const cfg = cfgRef.current;
      const res = await msalRef.current.loginPopup({ scopes: [cfg.workiqScope] });
      setAccount(res.account);
      setStatus("Signed in as " + res.account.username);
    } catch (err) {
      setStatus("Sign-in failed: " + err.message);
    }
  };

  const getToken = async () => {
    const cfg = cfgRef.current;
    const request = { account, scopes: [cfg.workiqScope] };
    try {
      const res = await msalRef.current.acquireTokenSilent(request);
      return res.accessToken;
    } catch {
      const res = await msalRef.current.acquireTokenPopup({ scopes: [cfg.workiqScope] });
      return res.accessToken;
    }
  };

  const handleServerMessage = (msg) => {
    if (msg.type === "ready") {
      readyRef.current = true;
      setStatus("Listening... start speaking");
    } else if (msg.type === "audio") {
      const f32 = base64ToFloat32(msg.data);
      playbackRef.current?.port.postMessage(f32.buffer, [f32.buffer]);
    } else if (msg.type === "barge_in") {
      playbackRef.current?.port.postMessage("clear");
    } else if (msg.type === "user_text") {
      if (msg.text) addLog("You: " + msg.text);
    } else if (msg.type === "agent_text") {
      if (msg.text) addLog("Assistant: " + msg.text);
    } else if (msg.type === "tool_call") {
      addLog("(consulting Work IQ...)");
    } else if (msg.type === "error") {
      addLog("Error: " + msg.message);
    }
  };

  const start = async () => {
    try {
      setStatus("Acquiring Work IQ token...");
      const token = await getToken();

      const ctx = new AudioContext({ sampleRate: 24000 });
      ctxRef.current = ctx;
      await ctx.audioWorklet.addModule("/capture-processor.js");
      await ctx.audioWorklet.addModule("/playback-processor.js");

      const playback = new AudioWorkletNode(ctx, "playback-processor");
      playback.connect(ctx.destination);
      playbackRef.current = playback;

      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;
      readyRef.current = false;

      ws.onopen = () => ws.send(JSON.stringify({ workiq_token: token }));
      ws.onmessage = (e) => handleServerMessage(JSON.parse(e.data));
      ws.onclose = () => setStatus("Session ended.");
      ws.onerror = () => setStatus("WebSocket error.");

      const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 } });
      streamRef.current = stream;
      const source = ctx.createMediaStreamSource(stream);
      const capture = new AudioWorkletNode(ctx, "capture-processor");
      capture.port.onmessage = (e) => {
        if (readyRef.current && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "audio", data: abToBase64(e.data) }));
        }
      };
      source.connect(capture); // not connected to destination (avoid feedback)

      setLive(true);
      setStatus("Connecting session...");
    } catch (err) {
      setStatus("Could not start: " + err.message);
    }
  };

  const stop = async () => {
    try {
      wsRef.current?.send(JSON.stringify({ type: "stop" }));
    } catch {}
    wsRef.current?.close();
    streamRef.current?.getTracks().forEach((t) => t.stop());
    await ctxRef.current?.close().catch(() => {});
    setLive(false);
    setStatus("Stopped.");
  };

  return (
    <main style={{ maxWidth: 640, margin: "40px auto", padding: 24 }}>
      <h1 style={{ marginBottom: 4 }}>Qubix Voice</h1>
      <p style={{ opacity: 0.7, marginTop: 0 }}>
        Voice assistant that reaches Microsoft 365 through Work IQ as the signed-in user.
      </p>

      <div style={{ margin: "16px 0", padding: 12, background: "#171a2e", borderRadius: 8 }}>
        {status}
      </div>

      {!account ? (
        <button onClick={signIn} style={btn}>Sign in</button>
      ) : !live ? (
        <button onClick={start} style={btn}>Start talking</button>
      ) : (
        <button onClick={stop} style={{ ...btn, background: "#b0304a" }}>Stop</button>
      )}

      <div style={{ marginTop: 24 }}>
        {log.map((line, i) => (
          <div key={i} style={{ padding: "4px 0", borderBottom: "1px solid #23263c" }}>{line}</div>
        ))}
      </div>
    </main>
  );
}

const btn = {
  padding: "10px 20px",
  fontSize: 16,
  borderRadius: 8,
  border: "none",
  background: "#4560e0",
  color: "white",
  cursor: "pointer",
};
