/* 最小 CSInterface：CEP 面板只需要 evalScript。
   （完整版由 Adobe 提供，这里不引外部依赖。） */
function CSInterface() {}

CSInterface.prototype.evalScript = function (script, callback) {
  if (window.__adobe_cep__ && window.__adobe_cep__.evalScript) {
    window.__adobe_cep__.evalScript(script, callback || function () {});
  } else if (callback) {
    callback("ERR:不在 CEP 环境里（面板要在 PR 内打开）");
  }
};

CSInterface.prototype.getHostEnvironment = function () {
  try {
    if (window.__adobe_cep__ && window.__adobe_cep__.getHostEnvironment) {
      return JSON.parse(window.__adobe_cep__.getHostEnvironment());
    }
  } catch (e) {}
  return {};
};

CSInterface.prototype.getExtensionID = function () {
  return "com.smartcut.prbridge.panel";
};
