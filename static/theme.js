/*
 * ライトモード／ダークモードの切り替え。
 * 選んだモードはこのブラウザに保存するので、ログアウト・ログインし直しても毎回切り替える必要はない。
 * <head> の中で読み込むことで、ページが表示される前にモードが決まり、画面がチラつかない。
 */
(function () {
  var KEY = 'matching-board-theme';
  var root = document.documentElement;

  function saved() {
    try { return localStorage.getItem(KEY); } catch (e) { return null; }
  }

  function apply(theme) {
    root.setAttribute('data-theme', theme === 'light' ? 'light' : 'dark');
  }

  apply(saved());

  function updateButton(btn) {
    var light = root.getAttribute('data-theme') === 'light';
    btn.textContent = light ? '🌙 ダークモード' : '☀️ ライトモード';
    btn.setAttribute('aria-label', light ? 'ダークモードに切り替える' : 'ライトモードに切り替える');
  }

  document.addEventListener('DOMContentLoaded', function () {
    var btn = document.getElementById('theme-toggle');
    if (!btn) return;
    updateButton(btn);
    btn.addEventListener('click', function () {
      var next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
      apply(next);
      try { localStorage.setItem(KEY, next); } catch (e) {}
      updateButton(btn);
    });
  });
})();
