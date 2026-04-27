import { useState, useRef, useEffect } from "react";

const SUGGESTED = [
  "How many pods are running?",
  "What is the status of all nodes?",
  "Are there any pods in CrashLoopBackOff?",
  "What namespaces exist?",
  "Show me all deployments",
  "Is ArgoCD healthy?",
];

const API_URL = "/api";

const PROVIDERS = [
  { id: "ollama", label: "Ollama", icon: "🦙", desc: "Local · Slow" },
  { id: "claude", label: "Claude", icon: "✳️", desc: "Cloud · Fast" },
];

export default function App() {
  const [messages, setMessages] = useState([
    {
      role: "assistant",
      text: "Hi! I'm your Kubernetes cluster assistant. Ask me anything about your cluster.",
      provider: null,
    },
  ]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [provider, setProvider] = useState("ollama");
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  const ask = async (question) => {
    if (!question.trim() || loading) return;
    const q = question.trim();
    setInput("");
    setMessages((m) => [...m, { role: "user", text: q, provider }]);
    setLoading(true);

    try {
      const res = await fetch(`${API_URL}/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q, provider }),
      });


      const data = await res.json();
if (!res.ok) {
  setMessages((m) => [...m, { role: "assistant", text: data.detail || "⚠️ Error.", provider }]);
  setLoading(false);
  return;
}
setMessages((m) => [...m, {
  role: "assistant",
  text: data.answer || "No response.",
  provider: data.provider,
}]);


    } catch (e) {
      setMessages((m) => [...m, { role: "assistant", text: "⚠️ Error reaching the API.", provider: null }]);
    }
    setLoading(false);
  };

  return (
    <div style={{
      minHeight: "100vh",
      background: "linear-gradient(135deg, #0f0f1a 0%, #1a1a2e 50%, #16213e 100%)",
      display: "flex", flexDirection: "column",
      fontFamily: "'Inter', 'Segoe UI', sans-serif", color: "#e2e8f0",
    }}>
      {/* Header */}
      <div style={{
        padding: "16px 24px", borderBottom: "1px solid rgba(99,179,237,0.2)",
        background: "rgba(15,15,26,0.8)", display: "flex", alignItems: "center",
        justifyContent: "space-between",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <div style={{
            width: 36, height: 36, borderRadius: 8,
            background: "linear-gradient(135deg, #326ce5, #63b3ed)",
            display: "flex", alignItems: "center", justifyContent: "center", fontSize: 18,
          }}>⎈</div>
          <div>
            <div style={{ fontWeight: 700, fontSize: 16, color: "#63b3ed" }}>Cluster AI</div>
            <div style={{ fontSize: 12, color: "#718096" }}>catdevops.net · natural language interface</div>
          </div>
        </div>

        {/* Provider toggle */}
        <div style={{ display: "flex", gap: 6, background: "rgba(255,255,255,0.05)", padding: 4, borderRadius: 10 }}>
          {PROVIDERS.map((p) => (
            <button
              key={p.id}
              onClick={() => setProvider(p.id)}
              style={{
                padding: "6px 14px", borderRadius: 8, border: "none", cursor: "pointer",
                fontSize: 12, fontWeight: 600, transition: "all 0.2s",
                background: provider === p.id
                  ? p.id === "claude"
                    ? "linear-gradient(135deg, #d97706, #f59e0b)"
                    : "linear-gradient(135deg, #326ce5, #63b3ed)"
                  : "transparent",
                color: provider === p.id ? "white" : "#718096",
              }}
            >
              {p.icon} {p.label}
              <span style={{ marginLeft: 6, fontWeight: 400, opacity: 0.8 }}>{p.desc}</span>
            </button>
          ))}
        </div>
      </div>

      {/* Suggested */}
      <div style={{ padding: "12px 24px", display: "flex", gap: 8, flexWrap: "wrap", borderBottom: "1px solid rgba(99,179,237,0.1)" }}>
        {SUGGESTED.map((s) => (
          <button key={s} onClick={() => ask(s)} style={{
            fontSize: 12, padding: "6px 12px", borderRadius: 20,
            background: "rgba(99,179,237,0.1)", color: "#90cdf4",
            border: "1px solid rgba(99,179,237,0.2)", cursor: "pointer",
          }}>{s}</button>
        ))}
      </div>

      {/* Messages */}
      <div style={{ flex: 1, overflowY: "auto", padding: "24px", display: "flex", flexDirection: "column", gap: 16 }}>
        {messages.map((m, i) => (
          <div key={i} style={{ display: "flex", justifyContent: m.role === "user" ? "flex-end" : "flex-start" }}>
            {m.role === "assistant" && (
              <div style={{
                width: 28, height: 28, borderRadius: 6, marginRight: 10, flexShrink: 0,
                background: "linear-gradient(135deg, #326ce5, #63b3ed)",
                display: "flex", alignItems: "center", justifyContent: "center", fontSize: 14,
              }}>⎈</div>
            )}
            <div>
              <div style={{
                maxWidth: "70%", padding: "12px 16px",
                borderRadius: m.role === "user" ? "18px 18px 4px 18px" : "18px 18px 18px 4px",
                background: m.role === "user" ? "linear-gradient(135deg, #326ce5, #4a90d9)" : "rgba(255,255,255,0.05)",
                border: m.role === "user" ? "none" : "1px solid rgba(99,179,237,0.15)",
                fontSize: 14, lineHeight: 1.6, whiteSpace: "pre-wrap",
              }}>{m.text}</div>
              {/* Provider badge */}
              {m.provider && (
                <div style={{ fontSize: 10, color: "#4a5568", marginTop: 4, paddingLeft: 4 }}>
                  {m.provider === "claude" ? "✳️ Claude" : "🦙 Ollama"}
                </div>
              )}
            </div>
          </div>
        ))}
        {loading && (
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div style={{
              width: 28, height: 28, borderRadius: 6,
              background: "linear-gradient(135deg, #326ce5, #63b3ed)",
              display: "flex", alignItems: "center", justifyContent: "center", fontSize: 14,
            }}>⎈</div>
            <div style={{
              padding: "12px 16px", borderRadius: "18px 18px 18px 4px",
              background: "rgba(255,255,255,0.05)", border: "1px solid rgba(99,179,237,0.15)",
              display: "flex", gap: 6, alignItems: "center",
            }}>
              {[0,1,2].map(i => (
                <div key={i} style={{
                  width: 8, height: 8, borderRadius: "50%", background: "#63b3ed",
                  animation: "pulse 1.2s ease-in-out infinite",
                  animationDelay: `${i * 0.2}s`, opacity: 0.7,
                }} />
              ))}
              <span style={{ fontSize: 12, color: "#718096", marginLeft: 4 }}>
                {provider === "claude" ? "Asking Claude..." : "Asking Ollama..."}
              </span>
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <div style={{
        padding: "16px 24px", borderTop: "1px solid rgba(99,179,237,0.2)",
        background: "rgba(15,15,26,0.8)", display: "flex", gap: 12,
      }}>
        <input
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => e.key === "Enter" && ask(input)}
          placeholder={`Ask about your cluster (${provider === "claude" ? "Claude" : "Ollama"})...`}
          style={{
            flex: 1, padding: "12px 16px", borderRadius: 12,
            background: "rgba(255,255,255,0.07)", border: "1px solid rgba(99,179,237,0.2)",
            color: "#e2e8f0", fontSize: 14, outline: "none",
          }}
        />
        <button onClick={() => ask(input)} disabled={loading || !input.trim()} style={{
          padding: "12px 20px", borderRadius: 12,
          background: loading || !input.trim() ? "rgba(99,179,237,0.2)" : "linear-gradient(135deg, #326ce5, #63b3ed)",
          border: "none", color: "white", cursor: loading || !input.trim() ? "not-allowed" : "pointer",
          fontSize: 18,
        }}>➤</button>
      </div>
      <style>{`@keyframes pulse { 0%,100%{transform:scale(1);opacity:0.7} 50%{transform:scale(1.3);opacity:1} }`}</style>
    </div>
  );
}
