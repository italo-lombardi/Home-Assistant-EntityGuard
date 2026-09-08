/**
 * Entity Guard Card v0.1.2
 * Custom Lovelace card for the Home Assistant Entity Guard integration.
 *
 * Config:
 *   type: custom:entity-guard-card
 *   rule_id: <config_entry_id>   # required
 *   title: <optional override>
 */

const CARD_VERSION = "0.1.1";

console.info(
  `%c ENTITY-GUARD-CARD %c v${CARD_VERSION} %c — github.com/italo-lombardi `,
  "color: white; background: #3f51b5; font-weight: bold; padding: 2px 6px; border-radius: 3px 0 0 3px;",
  "color: #3f51b5; background: #e8eaf6; font-weight: bold; padding: 2px 6px;",
  "color: #9e9e9e; background: #e8eaf6; padding: 2px 6px; border-radius: 0 3px 3px 0;",
);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "entity-guard-card",
  name: "Entity Guard Card",
  description: "Status, controls, and history for an Entity Guard rule.",
  preview: true,
  documentationURL:
    "https://github.com/italo-lombardi/Home-Assistant-EntityGuard",
});

// Canonical no-build LitElement bootstrap — matches thomasloven/lovelace-card-tools pattern.
// home-assistant-main and hui-view are in HA's initial bundle; always defined before card JS runs.
// Synchronous get() avoids the iOS WKWebView timing issues caused by whenDefined("ha-panel-lovelace")
// (ha-panel-lovelace is lazy-loaded and may resolve late or not at all on the Companion App).
const LitElement = Object.getPrototypeOf(
  customElements.get("home-assistant-main") || customElements.get("hui-view"),
);
const html = LitElement.prototype.html;
const nothing = LitElement.prototype.nothing ?? "";
const css =
  LitElement.prototype.css ||
  (() => {
    class CSSResult {
      constructor(cssText) {
        this.cssText = cssText;
        this._styleSheet = null;
      }
      get styleSheet() {
        if (this._styleSheet === null && window.CSSStyleSheet) {
          try {
            this._styleSheet = new CSSStyleSheet();
            this._styleSheet.replaceSync(this.cssText);
          } catch (e) {
            this._styleSheet = null;
          }
        }
        return this._styleSheet;
      }
      toString() {
        return this.cssText;
      }
    }
    return (strings, ...values) =>
      new CSSResult(
        strings.reduce(
          (acc, str, i) =>
            acc + str + (values[i] != null ? String(values[i]) : ""),
          "",
        ),
      );
  })();

const STATUS_COLORS = {
  error: "#f44336",
  disabled: "#9e9e9e",
  master_disabled: "#616161",
  suppressed: "#ff9800",
  enforcing: "#2196f3",
  cooldown: "#ffc107",
  armed: "#4caf50",
  conditional: "#bdbdbd",
  starting: "#03a9f4",
  pending: "#ff5722",
};

const STATUS_LABELS = {
  error: "Error",
  disabled: "Disabled",
  master_disabled: "Master disabled",
  suppressed: "Suppressed",
  enforcing: "Enforcing",
  cooldown: "Cooldown",
  armed: "Armed",
  conditional: "Waiting on conditions",
  starting: "Starting",
  pending: "About to enforce",
};

const cardStyles = css`
  :host {
    --eg-text-primary: var(--primary-text-color, #212121);
    --eg-text-secondary: var(--secondary-text-color, #727272);
    --eg-divider: var(--divider-color, rgba(0, 0, 0, 0.12));
  }
  ha-card {
    overflow: hidden;
  }
  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 14px 16px 10px;
    gap: 10px;
  }
  .title {
    font-size: 16px;
    font-weight: 500;
    color: var(--eg-text-primary);
    flex: 1;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .badge {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: #fff;
    padding: 3px 8px;
    border-radius: 10px;
    white-space: nowrap;
  }
  .divider {
    height: 1px;
    background: var(--eg-divider);
    margin: 0 16px;
  }
  .row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 16px;
    font-size: 13px;
  }
  .row .label {
    color: var(--eg-text-secondary);
  }
  .row .value {
    color: var(--eg-text-primary);
    font-weight: 500;
    text-align: right;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 60%;
  }
  .stats {
    display: flex;
    padding: 8px 16px;
    gap: 8px;
  }
  .stat {
    flex: 1;
    background: var(--secondary-background-color, #f5f5f5);
    border-radius: 6px;
    padding: 8px 10px;
    text-align: center;
  }
  .stat-value {
    font-size: 18px;
    font-weight: 600;
    color: var(--eg-text-primary);
  }
  .stat-label {
    font-size: 11px;
    color: var(--eg-text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-top: 2px;
  }
  .entities {
    padding: 4px 16px 8px;
  }
  .section-title {
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--eg-text-secondary);
    padding: 8px 0 4px;
  }
  .entity-row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 0;
    font-size: 13px;
  }
  .entity-name {
    flex: 1;
    color: var(--eg-text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .entity-state {
    color: var(--eg-text-secondary);
    white-space: nowrap;
  }
  .actions {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    padding: 8px 16px 14px;
  }
  button.action {
    flex: 1;
    min-width: 90px;
    padding: 6px 10px;
    font-size: 12px;
    font-weight: 500;
    border: 1px solid var(--eg-divider);
    border-radius: 4px;
    background: var(--card-background-color, #fff);
    color: var(--primary-text-color, #212121);
    cursor: pointer;
    transition: opacity 0.2s;
  }
  button.action:hover {
    opacity: 0.8;
  }
  button.action.primary {
    background: var(--primary-color, #03a9f4);
    color: var(--text-primary-color, #fff);
    border-color: transparent;
  }
  button.action.warn {
    background: var(--warning-color, #ff9800);
    color: var(--text-primary-color, #fff);
    border-color: transparent;
  }
  button.action.separator {
    margin-left: auto;
  }
  .toggle {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .error {
    padding: 16px;
    color: var(--error-color, #db4437);
    font-size: 13px;
  }
  .error-banner {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    margin: 8px 16px 0;
    padding: 8px 12px;
    background: rgba(244, 67, 54, 0.1);
    border-left: 3px solid #f44336;
    border-radius: 4px;
    color: var(--primary-text-color);
  }
  .error-banner ha-icon {
    color: #f44336;
    flex-shrink: 0;
    margin-top: 2px;
  }
  .error-title {
    font-weight: 500;
    font-size: 13px;
  }
  .error-detail {
    font-size: 12px;
    color: var(--secondary-text-color);
    margin-top: 2px;
    word-break: break-word;
  }
  @keyframes eg-shimmer {
    0% {
      background-position: -200% 0;
    }
    100% {
      background-position: 200% 0;
    }
  }
  .skeleton-line,
  .skeleton-badge,
  .skeleton-stat {
    display: inline-block;
    border-radius: 4px;
    background: linear-gradient(
      90deg,
      var(--secondary-background-color, #eee) 0%,
      var(--divider-color, rgba(0, 0, 0, 0.12)) 50%,
      var(--secondary-background-color, #eee) 100%
    );
    background-size: 200% 100%;
    animation: eg-shimmer 1.4s ease-in-out infinite;
  }
  .skeleton-line {
    height: 12px;
    width: 80px;
  }
  .skeleton-line.wide {
    width: 140px;
  }
  .skeleton-badge {
    height: 18px;
    width: 72px;
    border-radius: 10px;
  }
  .skeleton-stat {
    height: 22px;
    width: 60px;
    margin: 0 auto 4px;
  }
`;

class EntityGuardCard extends LitElement {
  static get properties() {
    return {
      hass: { attribute: false },
      _config: { state: true },
    };
  }

  constructor() {
    super();
    this._refsCache = null;
    this._refsCacheKey = null;
    this._mountedAt = Date.now();
    this._loadingTimer = null;
  }

  connectedCallback() {
    super.connectedCallback?.();
    this._mountedAt = Date.now();
    if (this._loadingTimer) clearTimeout(this._loadingTimer);
    this._loadingTimer = setTimeout(() => {
      this._loadingTimer = null;
      this.requestUpdate();
    }, 4000);
  }

  disconnectedCallback() {
    super.disconnectedCallback?.();
    if (this._loadingTimer) {
      clearTimeout(this._loadingTimer);
      this._loadingTimer = null;
    }
  }

  static get styles() {
    return cardStyles;
  }

  static getConfigElement() {
    return document.createElement("entity-guard-card-editor");
  }

  static getStubConfig(hass) {
    const states = hass?.states || {};
    const entities = hass?.entities || {};
    const devices = hass?.devices || {};
    const seen = new Map();
    for (const [entityId, st] of Object.entries(states)) {
      if (!entityId.startsWith("sensor.")) continue;
      if (!Array.isArray(st?.attributes?.target_entities)) continue;
      const entry = entities[entityId];
      const entryId = entry?.config_entry_id;
      if (!entryId || seen.has(entryId)) continue;
      const dev = entry?.device_id ? devices[entry.device_id] : null;
      const friendly = (st.attributes.friendly_name || "").replace(
        /\s*Status$/i,
        "",
      );
      seen.set(entryId, dev?.name_by_user || dev?.name || friendly || entryId);
    }
    const sorted = Array.from(seen.entries()).sort((a, b) =>
      a[1].localeCompare(b[1]),
    );
    return { rule_id: sorted[0]?.[0] || "", title: "" };
  }

  setConfig(config) {
    if (!config || typeof config !== "object") {
      throw new Error("entity-guard-card: invalid config.");
    }
    this._config = { ...config };
  }

  getCardSize() {
    return 4;
  }

  /**
   * Locate all per-rule entities via device-registry lookup.
   * Each rule entry creates a device with identifier ("entity_guard", entry_id).
   * Match entities whose device_id equals that device's id.
   * Falls back to anchor entity prefix when only `entity` was given.
   */
  _findMatchIds() {
    const ruleId = this._config.rule_id;
    const anchor = this._config.entity;
    const entities = this.hass.entities || {};
    const devices = this.hass.devices || {};
    const entityKeys = Object.keys(entities);
    const deviceKeys = Object.keys(devices);

    let ruleDeviceId = null;
    if (ruleId) {
      for (const devId of deviceKeys) {
        const ids = devices[devId]?.identifiers;
        if (!Array.isArray(ids)) continue;
        for (const pair of ids) {
          if (
            Array.isArray(pair) &&
            pair[0] === "entity_guard" &&
            pair[1] === ruleId
          ) {
            ruleDeviceId = devId;
            break;
          }
        }
        if (ruleDeviceId) break;
      }
    }

    const cacheKey = `${ruleId || ""}|${anchor || ""}|${ruleDeviceId || ""}|${entityKeys.length}|${deviceKeys.length}`;
    if (this._refsCacheKey === cacheKey && this._refsCache) {
      return this._refsCache;
    }

    const matches = [];
    for (const entityId of entityKeys) {
      const entry = entities[entityId];
      if (entry?.platform !== "entity_guard") continue;
      if (ruleDeviceId) {
        if (entry?.device_id !== ruleDeviceId) continue;
      } else if (anchor) {
        if (!entityId.startsWith(this._anchorPrefix(anchor))) continue;
      } else {
        continue;
      }
      matches.push(entityId);
    }

    if (matches.length === 0 && anchor && entityKeys.length > 0) {
      const prefix = this._anchorPrefix(anchor);
      for (const entityId of Object.keys(this.hass.states || {})) {
        if (entityId.startsWith(prefix)) matches.push(entityId);
      }
    }

    if (matches.length > 0) {
      this._refsCache = matches;
      this._refsCacheKey = cacheKey;
    }
    return matches;
  }

  _findRuleEntities() {
    if (!this.hass) return null;
    const out = {
      enabled: null,
      status: null,
      lastEnforced: null,
      countToday: null,
      countTotal: null,
      armed: null,
      active: null,
      inCooldown: null,
      reset: null,
      testEnforce: null,
      clearHistory: null,
      bound: [],
      targetState: null,
      triggerStates: null,
    };

    const entities = this.hass.entities || {};
    const matches = this._findMatchIds();

    for (const id of matches) {
      const st = this.hass.states[id];
      if (!st) continue;
      const entry = entities[id];
      const tk = entry?.translation_key || st.attributes?.translation_key;
      if (
        id.startsWith("switch.") &&
        (tk === "enabled" || /(_|^)enabled$/.test(id))
      ) {
        if (!tk || tk === "enabled") out.enabled = id;
      } else if (id.startsWith("sensor.") && tk === "status") {
        out.status = id;
      } else if (id.startsWith("sensor.") && tk === "last_enforced") {
        out.lastEnforced = id;
      } else if (id.startsWith("sensor.") && tk === "enforcement_count_today") {
        out.countToday = id;
      } else if (id.startsWith("sensor.") && tk === "enforcement_count_total") {
        out.countTotal = id;
      } else if (id.startsWith("binary_sensor.") && tk === "armed") {
        out.armed = id;
      } else if (id.startsWith("binary_sensor.") && tk === "active") {
        out.active = id;
      } else if (id.startsWith("binary_sensor.") && tk === "in_cooldown") {
        out.inCooldown = id;
      } else if (id.startsWith("button.") && tk === "reset") {
        out.reset = id;
      } else if (id.startsWith("button.") && tk === "test_enforce") {
        out.testEnforce = id;
      } else if (id.startsWith("button.") && tk === "clear_history") {
        out.clearHistory = id;
      }
    }

    const statusEntity = out.status ? this.hass.states[out.status] : null;
    const targets = statusEntity?.attributes?.target_entities;
    if (Array.isArray(targets)) {
      out.bound = targets;
    }
    out.targetState = statusEntity?.attributes?.target_state ?? null;
    out.triggerStates = statusEntity?.attributes?.trigger_states ?? null;

    return out;
  }

  _anchorPrefix(anchorId) {
    return anchorId.replace(/_enabled$/, "");
  }

  _stateValue(entityId, fallback = "—") {
    if (!entityId) return fallback;
    const st = this.hass.states[entityId];
    if (!st) return fallback;
    if (st.state === "unavailable" || st.state === "unknown") return fallback;
    return st.state;
  }

  _applyStateFormat(stateObj, rawValue) {
    if (!isNaN(parseFloat(rawValue))) return rawValue;
    let label = rawValue;
    if (this.hass.formatEntityState) {
      try {
        label = this.hass.formatEntityState(stateObj) ?? rawValue;
      } catch (e) {}
    }
    if (label.toLowerCase() === rawValue.toLowerCase()) return label;
    return `${label} (${rawValue})`;
  }

  // Returns display string for a live entity state.
  _formatStateDisplay(stateObj, rawValue) {
    if (!stateObj || rawValue == null) return rawValue ?? "unknown";
    return this._applyStateFormat(stateObj, rawValue);
  }

  // Returns display string for a hypothetical state value (e.g. required/target).
  // Builds a fake state object from the entity's existing attributes so
  // formatEntityState can resolve device_class translations.
  _formatHypotheticalState(entityId, rawValue) {
    if (rawValue == null) return "unknown";
    const existing = this.hass.states[entityId];
    if (!existing || !this.hass.formatEntityState) return rawValue;
    const fakeState = { ...existing, state: rawValue, attributes: { ...existing.attributes } };
    return this._applyStateFormat(fakeState, rawValue);
  }

  _attrValue(entityId, attr) {
    if (!entityId) return null;
    return this.hass.states[entityId]?.attributes?.[attr] ?? null;
  }

  _formatTimestamp(value) {
    if (!value) return "Never";
    const d = new Date(value);
    if (isNaN(d.getTime())) return value;
    return d.toLocaleString();
  }

  _ruleName(refs) {
    if (this._config.title) return this._config.title;
    const statusEntity = refs.status ? this.hass.states[refs.status] : null;
    const friendly = statusEntity?.attributes?.friendly_name;
    if (friendly) return friendly.replace(/\s*Status$/i, "");
    return "Entity Guard Rule";
  }

  render() {
    if (!this._config) {
      return html`<ha-card
        ><div class="error">Card not configured.</div></ha-card
      >`;
    }
    if (!this.hass) {
      return this._renderSkeleton();
    }

    if (!this._config.rule_id && !this._config.entity) {
      return html`<ha-card>
        <div
          style="display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px 16px;gap:8px;color:var(--secondary-text-color,#727272);"
        >
          <ha-icon
            icon="mdi:shield-lock"
            style="--mdi-icon-size:40px;color:var(--primary-color,#3f51b5)"
          ></ha-icon>
          <span style="font-weight:500;color:var(--primary-text-color)"
            >Entity Guard</span
          >
          <span style="font-size:13px">Select a rule in the card editor</span>
        </div>
      </ha-card>`;
    }

    const refs = this._findRuleEntities();
    if (!refs || (!refs.status && !refs.enabled)) {
      const sinceMount = Date.now() - this._mountedAt;
      if (sinceMount < 4000) {
        return this._renderSkeleton();
      }
      return html`<ha-card>
        <div class="error">
          No Entity Guard rule entities found for rule_id
          "${this._config.rule_id}".
        </div>
      </ha-card>`;
    }

    const status = this._stateValue(refs.status, "conditional");
    const color = STATUS_COLORS[status] || STATUS_COLORS.conditional;
    const statusEntity = refs.status ? this.hass.states[refs.status] : null;
    let label = STATUS_LABELS[status] || status;
    if (statusEntity && this.hass.formatEntityState) {
      try {
        label = this.hass.formatEntityState(statusEntity) || label;
      } catch (e) {}
    }
    const enabled = this.hass.states[refs.enabled]?.state === "on";
    const lastError = statusEntity?.attributes?.last_error;
    const consecutiveErrors = statusEntity?.attributes?.consecutive_errors;

    return html`
      <ha-card>
        <div class="header">
          <span class="title">${this._ruleName(refs)}</span>
          <span class="badge" style="background-color: ${color}">${label}</span>
        </div>
        <div class="divider"></div>
        ${status === "error" && lastError
          ? html`<div class="error-banner">
              <ha-icon icon="mdi:alert-circle"></ha-icon>
              <div>
                <div class="error-title">
                  ${consecutiveErrors} consecutive
                  failure${consecutiveErrors === 1 ? "" : "s"}
                </div>
                <div class="error-detail">${lastError}</div>
              </div>
            </div>`
          : nothing}
        ${this._renderToggle(refs, enabled)}
        ${this._config.show_stats !== false ? this._renderStats(refs) : nothing}
        ${this._config.show_last_enforced !== false
          ? this._renderInfo(refs)
          : nothing}
        ${this._config.show_entities !== false
          ? this._renderBound(refs)
          : nothing}
        ${this._config.show_conditions ? this._renderConditions(refs) : nothing}
        ${this._config.show_actions ? this._renderActions(refs) : nothing}
      </ha-card>
    `;
  }

  _renderSkeleton() {
    const title = this._config.title;
    return html`
      <ha-card aria-busy="true">
        <div class="header">
          ${title
            ? html`<span class="title">${title}</span>`
            : html`<span class="skeleton-line wide" aria-hidden="true"></span>`}
          <span class="skeleton-badge" aria-hidden="true"></span>
        </div>
        <div class="divider"></div>
        <div class="row">
          <span class="label">Enabled</span>
          <span
            class="skeleton-line"
            style="width:40px"
            aria-hidden="true"
          ></span>
        </div>
        <div class="stats">
          <div class="stat">
            <div class="skeleton-stat" aria-hidden="true"></div>
            <div class="stat-label">Enforcements today</div>
          </div>
          <div class="stat">
            <div class="skeleton-stat" aria-hidden="true"></div>
            <div class="stat-label">Enforcements total</div>
          </div>
        </div>
        <div class="row">
          <span class="label">Last enforced</span>
          <span class="skeleton-line" aria-hidden="true"></span>
        </div>
      </ha-card>
    `;
  }

  _renderToggle(refs, enabled) {
    if (!refs.enabled) return nothing;
    return html`
      <div class="row">
        <span class="label">Enabled</span>
        <div class="toggle">
          <ha-switch
            .checked=${enabled}
            @change=${(e) =>
              this._toggleEnabled(refs.enabled, e.target.checked)}
          ></ha-switch>
        </div>
      </div>
    `;
  }

  _renderStats(refs) {
    const today = this._stateValue(refs.countToday, "0");
    const total = this._stateValue(refs.countTotal, "0");
    return html`
      <div class="stats">
        <div class="stat">
          <div class="stat-value">${today}</div>
          <div class="stat-label">Enforcements today</div>
        </div>
        <div class="stat">
          <div class="stat-value">${total}</div>
          <div class="stat-label">Enforcements total</div>
        </div>
      </div>
    `;
  }

  _renderInfo(refs) {
    const last = this._stateValue(refs.lastEnforced, null);
    return html`
      <div class="row">
        <span class="label">Last enforced</span>
        <span class="value">${this._formatTimestamp(last)}</span>
      </div>
    `;
  }

  _renderBound(refs) {
    if (!refs.bound || refs.bound.length === 0) return nothing;
    const triggerStates = refs.triggerStates;
    return html`
      <div class="entities">
        <div class="section-title">Bound entities (${refs.bound.length})</div>
        ${refs.bound.map((id) => {
          const st = this.hass.states[id];
          const name = st?.attributes?.friendly_name || id;
          const state = st ? st.state : "unknown";
          const displayState = this._formatStateDisplay(st, state);
          const displayTarget = this._formatHypotheticalState(id, refs.targetState);
          const compliant = !triggerStates || !triggerStates.includes(state);
          return html`
            <div class="entity-row">
              <span class="entity-name" title="${id}">${name}</span>
              <span class="entity-state">
                ${compliant
                  ? html`${displayState}
                      <span style="color:var(--success-color,#4caf50)">✓</span>`
                  : html`<span style="color:var(--warning-color,#ff9800)"
                      >${displayState} → ${displayTarget} ⚠</span
                    >`}
              </span>
            </div>
          `;
        })}
      </div>
    `;
  }

  _renderConditions(refs) {
    const statusEntity = refs.status ? this.hass.states[refs.status] : null;
    const flags = statusEntity?.attributes?.flags;
    if (!Array.isArray(flags) || flags.length === 0) return nothing;
    return html`
      <div class="entities">
        <div class="section-title">Conditions (${flags.length})</div>
        ${flags.map((f) => {
          const st = this.hass.states[f.entity];
          const name = st?.attributes?.friendly_name || f.entity;
          const displayCurrent = this._formatStateDisplay(st, f.current ?? "unknown");
          const displayRequired = this._formatHypotheticalState(f.entity, f.required ?? null);
          return html`
            <div class="entity-row">
              <span class="entity-name" title="${f.entity}">${name}</span>
              <span class="entity-state">
                ${f.matches
                  ? html`${displayCurrent}
                      <span style="color:var(--success-color,#4caf50)">✓</span>`
                  : html`<span style="color:var(--warning-color,#ff9800)"
                      >${displayCurrent} → ${displayRequired} ⚠</span
                    >`}
              </span>
            </div>
          `;
        })}
      </div>
    `;
  }

  _renderActions(refs) {
    return html`
      <div class="entities">
        <div class="section-title">Actions</div>
        <div class="actions">
          <button
            class="action primary"
            @click=${() => this._press(refs.testEnforce)}
            ?disabled=${!refs.testEnforce}
          >
            Test Enforce
          </button>
          ${refs.inCooldown && this.hass.states[refs.inCooldown]?.state === "on"
            ? html` <button
                class="action"
                @click=${() => this._press(refs.reset)}
                ?disabled=${!refs.reset}
              >
                Reset Cooldowns
              </button>`
            : nothing}
          <button
            class="action"
            @click=${() => this._press(refs.clearHistory)}
            ?disabled=${!refs.clearHistory}
          >
            Clear History
          </button>
          <button
            class="action warn separator"
            @click=${() => this._suppress(60)}
          >
            Suppress 1h
          </button>
        </div>
      </div>
    `;
  }

  async _toggleEnabled(entityId, checked) {
    if (!entityId) return;
    await this.hass.callService("switch", checked ? "turn_on" : "turn_off", {
      entity_id: entityId,
    });
  }

  async _press(entityId) {
    if (!entityId) return;
    await this.hass.callService("button", "press", { entity_id: entityId });
  }

  async _suppress(minutes) {
    const ruleId = this._resolveRuleId();
    if (!ruleId) {
      console.warn("entity-guard-card: cannot suppress without rule_id");
      return;
    }
    await this.hass.callService("entity_guard", "suppress", {
      rule_id: ruleId,
      duration_minutes: minutes,
    });
  }

  _resolveRuleId() {
    if (this._config.rule_id) return this._config.rule_id;
    const anchor = this._config.entity;
    if (!anchor) return null;
    const entry = this.hass?.entities?.[anchor];
    return entry?.config_entry_id || null;
  }
}

customElements.define("entity-guard-card", EntityGuardCard);

// --- Minimal Editor ---

class EntityGuardCardEditor extends LitElement {
  static get properties() {
    return {
      hass: { attribute: false },
      _config: { state: true },
      _entries: { state: true },
    };
  }

  constructor() {
    super();
    this._entries = null;
    this._loading = false;
  }

  async _loadEntries() {
    if (!this.hass || this._entries !== null || this._loading) return;
    this._loading = true;
    try {
      const all = await this.hass.callWS({
        type: "config_entries/get",
        domain: "entity_guard",
      });
      this._entries = (all || [])
        .filter((e) => e.source !== "import")
        .map((e) => ({ id: e.entry_id, name: e.title || e.entry_id }))
        .sort((a, b) => a.name.localeCompare(b.name));
    } catch (_) {
      this._entries = [];
    } finally {
      this._loading = false;
      this.requestUpdate();
    }
  }

  updated(changed) {
    if (changed.has("hass")) this._loadEntries();
  }

  static get styles() {
    return css`
      .editor-row {
        margin-bottom: 12px;
      }
      .editor-row label {
        display: block;
        font-weight: 500;
        margin-bottom: 4px;
      }
      .editor-row input[type="text"],
      .editor-row select {
        width: 100%;
        padding: 8px;
        border: 1px solid var(--divider-color, #ccc);
        border-radius: 4px;
        box-sizing: border-box;
        background: var(--card-background-color, #fff);
        color: var(--primary-text-color, #212121);
      }
      .help {
        font-size: 12px;
        color: var(--secondary-text-color, #727272);
        margin-top: 4px;
      }
    `;
  }

  setConfig(config) {
    this._config = config;
  }

  render() {
    if (!this._config) return html``;
    const rules = this._entries || [];
    const loading = this._entries === null || this._loading;
    const current = this._config.rule_id || "";
    const currentMissing = current && !rules.some((r) => r.id === current);
    return html`
      <div style="padding: 16px;">
        <div class="editor-row">
          <label>Rule</label>
          ${loading
            ? html`<select disabled>
                <option>Loading rules…</option>
              </select>`
            : rules.length === 0
              ? html`<select disabled>
                  <option>No Entity Guard rules configured</option>
                </select>`
              : html`<select
                  .value=${current}
                  @change=${(e) => this._update("rule_id", e.target.value)}
                >
                  ${!current
                    ? html`<option value="" disabled selected>
                        Select a rule…
                      </option>`
                    : nothing}
                  ${rules.map(
                    (r) =>
                      html`<option value=${r.id} ?selected=${r.id === current}>
                        ${r.name}
                      </option>`,
                  )}
                  ${currentMissing
                    ? html`<option value=${current} selected>
                        Unknown rule (${current})
                      </option>`
                    : nothing}
                </select>`}
          <div class="help">
            Pick the Entity Guard rule this card should display.
          </div>
        </div>
        <div class="editor-row">
          <label>Title (optional)</label>
          <input
            type="text"
            .value=${this._config.title || ""}
            @input=${(e) => this._update("title", e.target.value || undefined)}
            placeholder="Override displayed name"
          />
        </div>
        <div
          class="editor-row"
          style="display:flex;align-items:center;justify-content:space-between;"
        >
          <label style="margin-bottom:0">Show enforcement counters</label>
          <ha-switch
            .checked=${this._config.show_stats !== false}
            @change=${(e) => this._update("show_stats", e.target.checked)}
          ></ha-switch>
        </div>
        <div
          class="editor-row"
          style="display:flex;align-items:center;justify-content:space-between;"
        >
          <label style="margin-bottom:0">Show last enforced</label>
          <ha-switch
            .checked=${this._config.show_last_enforced !== false}
            @change=${(e) =>
              this._update("show_last_enforced", e.target.checked)}
          ></ha-switch>
        </div>
        <div
          class="editor-row"
          style="display:flex;align-items:center;justify-content:space-between;"
        >
          <label style="margin-bottom:0">Show bound entities</label>
          <ha-switch
            .checked=${this._config.show_entities !== false}
            @change=${(e) => this._update("show_entities", e.target.checked)}
          ></ha-switch>
        </div>
        <div
          class="editor-row"
          style="display:flex;align-items:center;justify-content:space-between;"
        >
          <label style="margin-bottom:0">Show conditions</label>
          <ha-switch
            .checked=${!!this._config.show_conditions}
            @change=${(e) => this._update("show_conditions", e.target.checked)}
          ></ha-switch>
        </div>
        <div
          class="editor-row"
          style="display:flex;align-items:center;justify-content:space-between;"
        >
          <label style="margin-bottom:0">Show actions</label>
          <ha-switch
            .checked=${!!this._config.show_actions}
            @change=${(e) => this._update("show_actions", e.target.checked)}
          ></ha-switch>
        </div>
      </div>
    `;
  }

  _update(key, value) {
    const next = { ...this._config, [key]: value };
    Object.keys(next).forEach((k) => {
      if (next[k] === undefined) delete next[k];
    });
    this._config = next;
    this.dispatchEvent(
      new CustomEvent("config-changed", {
        detail: { config: this._config },
        bubbles: true,
        composed: true,
      }),
    );
  }
}

customElements.define("entity-guard-card-editor", EntityGuardCardEditor);
