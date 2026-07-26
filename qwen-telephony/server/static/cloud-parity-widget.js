const controlPlaneOrigin = new URL(
  document.currentScript?.src || window.location.href,
  window.location.href,
).origin;
const liveKitClientModule = "https://cdn.jsdelivr.net/npm/livekit-client@2.21.0/dist/livekit-client.esm.mjs";

class LiveKitAgentWidget extends HTMLElement {
  connectedCallback() {
    this.audioElements = new Set();
    this.innerHTML = `
      <button type="button" part="button">开始语音对话</button>
      <span part="status" aria-live="polite">未连接</span>
    `;
    this.querySelector("button").addEventListener("click", () => this.toggle());
  }

  async toggle() {
    if (this.room) {
      await this.room.disconnect();
      this.cleanupRoom();
      return;
    }
    const configId = this.getAttribute("config-id");
    if (!configId) throw new Error("config-id is required");
    const button = this.querySelector("button");
    button.disabled = true;
    this.setStatus("连接中…");
    try {
      const apiBase = (this.getAttribute("api-base") || controlPlaneOrigin).replace(/\/$/, "");
      const response = await fetch(`${apiBase}/api/embed/${encodeURIComponent(configId)}/token`, {
        method: "POST",
        mode: "cors",
        credentials: "omit",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ participant_name: this.getAttribute("participant-name") || "Guest" }),
      });
      if (!response.ok) throw new Error(`token request failed: ${response.status}`);
      const auth = await response.json();
      const livekit = await import(liveKitClientModule);
      const room = new livekit.Room({ adaptiveStream: true, dynacast: true });
      room.on(livekit.RoomEvent.TrackSubscribed, (track) => {
        if (track.kind !== livekit.Track.Kind.Audio) return;
        const element = track.attach();
        element.hidden = true;
        document.body.appendChild(element);
        this.audioElements.add(element);
      });
      room.on(livekit.RoomEvent.TrackUnsubscribed, (track) => {
        track.detach().forEach((element) => {
          this.audioElements.delete(element);
          element.remove();
        });
      });
      room.on(livekit.RoomEvent.Disconnected, () => this.cleanupRoom());
      await room.connect(auth.url, auth.token);
      await room.localParticipant.setMicrophoneEnabled(true);
      if (auth.capabilities?.camera && this.hasAttribute("enable-camera")) {
        await room.localParticipant.setCameraEnabled(true);
      }
      this.room = room;
      this.setStatus("通话中");
    } catch (error) {
      this.setStatus("连接失败");
      this.dispatchEvent(new CustomEvent("voice-widget-error", { detail: error }));
      throw error;
    } finally {
      button.disabled = false;
    }
  }

  cleanupRoom() {
    this.audioElements?.forEach((element) => element.remove());
    this.audioElements?.clear();
    this.room = null;
    this.setStatus("未连接");
  }

  setStatus(value) {
    this.querySelector('[part="status"]').textContent = value;
  }
}

customElements.define("livekit-agent-widget", LiveKitAgentWidget);
