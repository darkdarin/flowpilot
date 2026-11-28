<script setup lang="ts">
// MCP servers of the runtime (ADR-025): their connection, sign-in and the tools agents get.
// OAuth sign-in opens the provider in a window through the project's https return page, which
// passes the answer back to this FlowPilot (through Home Assistant when under Ingress).
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import type { McpServer } from "../api";
import { ROOT } from "../api";
import { t } from "../i18n";
import { api, flash, mcpOpen } from "../store";

interface Draft {
  id?: string;
  name: string;
  title: string;
  url: string;
  transport: "http" | "sse";
  auth: "none" | "token" | "oauth";
  enabled: boolean;
  token: string;
  tokenSet: boolean;
  clientId: string;
  clientSecret: string;
  clientSecretSet: boolean;
  scope: string;
}

const servers = ref<McpServer[]>([]);
const returnPage = ref("");
const busy = ref(false);
const draft = ref<Draft | null>(null);
const opened = ref<Set<string>>(new Set());
const signingIn = ref<string | null>(null);
let timer: number | undefined;

async function load() {
  const data = await api.value!.mcp();
  servers.value = data.servers;
  returnPage.value = data.return_page;
  if (signingIn.value) {
    const s = data.servers.find((x) => x.id === signingIn.value);
    if (!s || s.state === "ready" || (s.error ?? "").startsWith("sign-in failed")) {
      signingIn.value = null;
    }
  }
}

function onMessage(event: MessageEvent) {
  if ((event.data as { type?: string })?.type === "flowpilot-mcp-login") void load();
}

onMounted(() => {
  void load().catch((err) => flash((err as Error).message, "error"));
  // states change in the background (connecting, signed in): keep the list fresh while open
  timer = window.setInterval(() => void load().catch(() => {}), 2000);
  window.addEventListener("message", onMessage);
});

onBeforeUnmount(() => {
  window.clearInterval(timer);
  window.removeEventListener("message", onMessage);
});

function edit(s?: McpServer) {
  draft.value = s
    ? {
        id: s.id,
        name: s.name,
        title: s.title,
        url: s.url,
        transport: s.transport,
        auth: s.auth,
        enabled: s.enabled,
        token: "",
        tokenSet: s.token_set,
        clientId: s.client_id,
        clientSecret: "",
        clientSecretSet: s.client_secret_set,
        scope: s.scope,
      }
    : {
        name: "",
        title: "",
        url: "",
        transport: "http",
        auth: "oauth",
        enabled: true,
        token: "",
        tokenSet: false,
        clientId: "",
        clientSecret: "",
        clientSecretSet: false,
        scope: "",
      };
}

async function save() {
  const d = draft.value;
  if (!d) return;
  busy.value = true;
  try {
    await api.value!.saveMcp({
      id: d.id,
      name: d.name.trim(),
      title: d.title.trim() || d.name.trim(),
      url: d.url.trim(),
      transport: d.transport,
      auth: d.auth,
      enabled: d.enabled,
      client_id: d.auth === "oauth" ? d.clientId.trim() : "",
      scope: d.auth === "oauth" ? d.scope.trim() : "",
      ...(d.token ? { token: d.token } : {}),
      ...(d.clientSecret ? { client_secret: d.clientSecret } : {}),
    });
    draft.value = null;
    await load();
  } catch (err) {
    flash((err as Error).message, "error");
  } finally {
    busy.value = false;
  }
}

async function remove(s: McpServer) {
  if (!window.confirm(t("mcp.confirmRemove", { name: s.title }))) return;
  await api.value!.deleteMcp(s.id);
  await load();
}

async function toggleServer(s: McpServer) {
  try {
    await api.value!.saveMcp({ ...s, enabled: !s.enabled });
    await load();
  } catch (err) {
    flash((err as Error).message, "error");
  }
}

async function toggleTool(s: McpServer, tool: string) {
  const off = new Set(s.disabled_tools);
  if (off.has(tool)) off.delete(tool);
  else off.add(tool);
  try {
    await api.value!.setMcpTools(s.id, [...off]);
    await load();
  } catch (err) {
    flash((err as Error).message, "error");
  }
}

async function reconnect(s: McpServer) {
  try {
    await api.value!.reconnectMcp(s.id);
    await load();
  } catch (err) {
    flash((err as Error).message, "error");
  }
}

/** Where the return page sends the provider's answer: Home Assistant's own address under
 * Ingress (its cookie would not come along on a cross-site return), else this runtime. */
function returnUrl(): string {
  if (location.pathname.includes("/api/hassio_ingress/")) {
    return `${location.origin}/api/flowpilot/oauth/callback`;
  }
  return `${location.origin}${ROOT}/api/mcp/oauth/callback`;
}

async function login(s: McpServer) {
  // open the window now, in the click: a window opened after an await is blocked as a popup
  const popup = window.open("about:blank", "flowpilot-mcp-login", "width=520,height=720");
  signingIn.value = s.id;
  try {
    const { url, return_page } = await api.value!.loginMcp(s.id);
    const page = new URL(return_page);
    page.searchParams.set("start", url);
    page.searchParams.set("return", returnUrl());
    if (popup) popup.location.href = page.href;
    else window.open(page.href, "_blank");
    await load();
  } catch (err) {
    popup?.close();
    signingIn.value = null;
    flash((err as Error).message, "error");
  }
}

async function logout(s: McpServer) {
  if (!window.confirm(t("mcp.confirmLogout", { name: s.title }))) return;
  await api.value!.logoutMcp(s.id);
  await load();
}

function stateText(s: McpServer): string {
  if (signingIn.value === s.id && s.state !== "ready") return t("mcp.state.signingIn");
  if (s.state === "ready") return t("mcp.state.ready", { n: s.tools.filter((x) => x.enabled).length, all: s.tools.length });
  return t(`mcp.state.${s.state}` as "mcp.state.connecting");
}

function toggleOpen(id: string) {
  const next = new Set(opened.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  opened.value = next;
}

const authHint = computed(() => (draft.value ? t(`mcp.authHint.${draft.value.auth}` as "mcp.authHint.none") : ""));
</script>

<template>
  <div class="modal" @click.self="mcpOpen = false" @keydown.esc="mcpOpen = false">
    <section class="dialog" role="dialog" aria-modal="true" :aria-label="t('mcp.title')">
      <h2>{{ t("mcp.title") }}</h2>
      <p class="hint">{{ t("mcp.hint") }}</p>

      <form v-if="draft" class="form" @submit.prevent="save">
        <label class="lbl">{{ t("mcp.titleField") }}</label>
        <input v-model="draft.title" class="field" :placeholder="t('mcp.titlePlaceholder')" />
        <label class="lbl">{{ t("mcp.name") }}</label>
        <input v-model="draft.name" class="field code" placeholder="notion" />
        <label class="lbl">{{ t("mcp.url") }}</label>
        <input v-model="draft.url" class="field code" placeholder="https://mcp.example.com/mcp" />
        <div class="field-row">
          <label class="lbl">{{ t("mcp.transport") }}</label>
          <select v-model="draft.transport" class="field small">
            <option value="http">Streamable HTTP</option>
            <option value="sse">SSE</option>
          </select>
          <label class="lbl">{{ t("mcp.auth") }}</label>
          <select v-model="draft.auth" class="field small">
            <option value="oauth">OAuth</option>
            <option value="token">{{ t("mcp.auth.token") }}</option>
            <option value="none">{{ t("mcp.auth.none") }}</option>
          </select>
        </div>
        <p class="hint">{{ authHint }}</p>
        <template v-if="draft.auth === 'token'">
          <label class="lbl">{{ t("mcp.token") }}</label>
          <input v-model="draft.token" class="field code" type="password" autocomplete="off" :placeholder="draft.tokenSet ? t('mcp.secretKept') : ''" />
        </template>
        <details v-if="draft.auth === 'oauth'" class="advanced">
          <summary>{{ t("mcp.oauthAdvanced") }}</summary>
          <p class="hint">{{ t("mcp.oauthAdvancedHint", { page: returnPage }) }}</p>
          <label class="lbl">Client ID</label>
          <input v-model="draft.clientId" class="field code" autocomplete="off" />
          <label class="lbl">Client secret</label>
          <input v-model="draft.clientSecret" class="field code" type="password" autocomplete="off" :placeholder="draft.clientSecretSet ? t('mcp.secretKept') : ''" />
          <label class="lbl">{{ t("mcp.scope") }}</label>
          <input v-model="draft.scope" class="field code" placeholder="read write" />
        </details>
        <label class="check"><input v-model="draft.enabled" type="checkbox" />{{ t("mcp.enabled") }}</label>
        <div class="row end">
          <button type="button" class="btn ghost" @click="draft = null">{{ t("graph.cancel") }}</button>
          <button type="submit" class="btn primary" :disabled="busy">{{ t("memory.save") }}</button>
        </div>
      </form>

      <template v-else>
        <ul v-if="servers.length" class="list">
          <li v-for="s in servers" :key="s.id" :class="{ off: !s.enabled }">
            <div class="row">
              <span class="dot" :class="s.state" />
              <strong>{{ s.title }}</strong>
              <code class="name">{{ s.name }}</code>
              <span class="desc">{{ stateText(s) }}</span>
              <label class="check"><input type="checkbox" :checked="s.enabled" @change="toggleServer(s)" />{{ t("skills.enabled") }}</label>
            </div>
            <p v-if="s.error && s.state !== 'ready'" class="error">{{ s.error }}</p>
            <p class="meta"><code>{{ s.url }}</code></p>
            <div class="row">
              <button v-if="s.auth === 'oauth' && s.enabled" type="button" class="btn small" :class="s.state === 'needs_auth' ? 'primary' : 'ghost'" :disabled="signingIn === s.id" @click="login(s)">
                {{ s.signed_in ? t("mcp.loginAgain") : t("mcp.login") }}
              </button>
              <button v-if="s.auth === 'oauth' && s.signed_in" type="button" class="btn ghost small" @click="logout(s)">{{ t("mcp.logout") }}</button>
              <button v-if="s.enabled" type="button" class="btn ghost small" @click="reconnect(s)">{{ t("mcp.reconnect") }}</button>
              <button v-if="s.tools.length" type="button" class="btn ghost small" @click="toggleOpen(s.id)">
                {{ opened.has(s.id) ? t("mcp.hideTools") : t("mcp.showTools") }}
              </button>
              <span class="grow" />
              <button type="button" class="btn ghost small" @click="edit(s)">{{ t("memory.edit") }}</button>
              <button type="button" class="btn ghost small danger" @click="remove(s)">{{ t("memory.remove") }}</button>
            </div>
            <ul v-if="opened.has(s.id)" class="tools">
              <li v-for="tool in s.tools" :key="tool.name" :class="{ off: !tool.enabled }">
                <label class="check"><input type="checkbox" :checked="tool.enabled" @change="toggleTool(s, tool.name)" /><code>{{ tool.name }}</code></label>
                <span class="badge" :class="tool.read_only ? 'ro' : 'rw'">{{ tool.read_only ? t("mcp.readOnly") : t("mcp.changes") }}</span>
                <span class="desc">{{ tool.description }}</span>
              </li>
            </ul>
          </li>
        </ul>
        <p v-else class="hint">{{ t("mcp.none") }}</p>
        <div class="row end">
          <button type="button" class="btn ghost" @click="edit()">{{ t("mcp.add") }}</button>
          <button type="button" class="btn ghost" @click="mcpOpen = false">{{ t("secrets.close") }}</button>
        </div>
      </template>
    </section>
  </div>
</template>

<style scoped>
.modal {
  position: fixed;
  inset: 0;
  display: grid;
  place-items: center;
  background: rgb(0 0 0 / 0.35);
  z-index: 20;
}

.dialog {
  width: min(760px, calc(100% - 32px));
  max-height: calc(100% - 64px);
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 22px;
  border-radius: 12px;
  background: var(--panel);
  border: 1px solid var(--line);
  box-shadow: var(--shadow);
}

h2 {
  margin: 0;
  font-size: 16px;
}

.hint {
  margin: 0;
  color: var(--muted);
  font-size: 12.5px;
  line-height: 1.45;
}

.form {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.lbl {
  font-size: 12px;
  color: var(--muted);
}

.field-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.field-row .field.small {
  flex: 0 1 170px;
}

.code {
  font-family: var(--mono, monospace);
  font-size: 12px;
}

.check {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--muted);
}

.advanced {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px;
  border: 1px dashed var(--line);
  border-radius: 8px;
}

.advanced summary {
  cursor: pointer;
  font-size: 12.5px;
}

.advanced[open] {
  display: flex;
}

.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.list > li {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 9px 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel-raised);
}

.list > li.off,
.tools > li.off {
  opacity: 0.6;
}

.row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.row.end {
  justify-content: flex-end;
}

.grow {
  flex: 1;
}

.name {
  color: var(--faint);
  font-size: 12px;
}

.desc {
  flex: 1;
  font-size: 12.5px;
  color: var(--muted);
}

.meta {
  margin: 0;
  font-size: 11.5px;
  color: var(--faint);
  word-break: break-all;
}

.error {
  margin: 0;
  font-size: 12px;
  color: var(--danger);
}

.dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--faint);
  flex: none;
}

.dot.ready {
  background: #2e9d5b;
}

.dot.connecting {
  background: #d9a521;
}

.dot.needs_auth,
.dot.error {
  background: var(--danger);
}

.tools {
  list-style: none;
  margin: 0;
  padding: 6px 0 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
  border-top: 1px solid var(--line);
}

.tools > li {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 12px;
}

.badge {
  flex: none;
  font-size: 10.5px;
  padding: 1px 6px;
  border-radius: 6px;
  border: 1px solid var(--line);
}

.badge.rw {
  color: var(--danger);
}

.danger {
  color: var(--danger);
}
</style>
