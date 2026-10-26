// Shared test harness for the Entity Guard card.
//
// The card (../../custom_components/entity_guard/frontend/
// entity-guard-card.js) is a no-build LitElement served to the browser as
// `type: module`. It derives its LitElement base at module-load time from
//   Object.getPrototypeOf(customElements.get("home-assistant-main"))
// and reads `.prototype.html` / `.css` / `.nothing` off it. In node none of
// those globals exist, so importing the card bare throws.
//
// We stub just enough of the browser before importing the card, then capture
// the card class from its own customElements.define() call (it is not exported
// — and must not be, since exporting would mean editing the shipped file).
//
// The html stub joins the template's `${}` interpolations with String.raw so a
// rendered fragment's text is assertable (this is the capability a future
// render test needs). The card reads `.css` and `.nothing` too, but with its
// own fallbacks (`css || …`, `nothing ?? ""`), so those need no stub here.

// Flatten a tagged-template call to its rendered text. Nested html`` fragments
// (produced by the same stub) carry a __html string; splice those in.
function htmlTag(strings, ...values) {
  const flat = values.map((v) =>
    v != null && typeof v === "object" && "__html" in v ? v.__html : v,
  );
  return { __html: String.raw({ raw: strings }, ...flat) };
}

// The card does `LitElement = Object.getPrototypeOf(customElements.get(...))`,
// so get() must return a subclass whose prototype (the superclass) carries the
// Lit members. class Sub extends Base ⇒ Object.getPrototypeOf(Sub) === Base.
// Only `.html` is read without a card-side fallback, so only it is stubbed.
class LitBase {}
LitBase.prototype.html = htmlTag;
class HaMainStub extends LitBase {}

const defined = {};
globalThis.window = globalThis.window || { customCards: [] };
globalThis.window.customCards = globalThis.window.customCards || [];
globalThis.customElements = {
  get: () => HaMainStub,
  define: (name, cls) => {
    defined[name] = cls;
  },
};
// The card logs a banner at import; keep test output clean.
globalThis.console.info = () => {};

await import(
  "../../custom_components/entity_guard/frontend/entity-guard-card.js"
);

export const Card = defined["entity-guard-card"];
if (!Card) throw new Error("card class not captured from customElements.define");
