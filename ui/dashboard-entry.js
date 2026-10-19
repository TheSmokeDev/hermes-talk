const SDK = window.__HERMES_PLUGIN_SDK__;
if (SDK && window.__HERMES_PLUGINS__) {
  const surface = createTalkSurface(SDK);
  if (window.__HERMES_TALK_TEST_HOOK__) window.__HERMES_TALK_TEST__ = surface;
  window.__HERMES_PLUGINS__.register("hermes-talk", surface.TalkPage);
}
