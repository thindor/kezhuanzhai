// 全站可转债搜索：单结果直跳详情页，多结果（如简拼歧义）展示候选下拉
function cbSearch(inputId, boxId) {
  var input = document.getElementById(inputId);
  var box = document.getElementById(boxId);
  if (!input || !box) { return; }
  var q = input.value.trim();
  if (!q) { box.style.display = 'none'; box.innerHTML = ''; return; }
  box.innerHTML = '<div class="cb-suggest-empty">搜索中…</div>';
  box.style.display = 'block';
  fetch('/api/search?q=' + encodeURIComponent(q))
    .then(function (r) { return r.json(); })
    .then(function (d) {
      var list = (d && d.results) || [];
      if (list.length === 0) {
        box.innerHTML = '<div class="cb-suggest-empty">未找到匹配的可转债</div>';
        box.style.display = 'block';
        return;
      }
      if (list.length === 1) {
        window.location.href = '/bond/' + encodeURIComponent(list[0].bond_code);
        return;
      }
      var html = '<div class="cb-suggest-hint">找到 ' + list.length + ' 只，请选择：</div>';
      list.forEach(function (b) {
        html += '<a class="cb-suggest-item" href="/bond/' + b.bond_code + '">' +
                '<span class="cb-si-name">' + (b.bond_name || b.bond_code) + '</span>' +
                '<span class="cb-si-code">' + b.bond_code + '</span>' +
                (b.stock_name ? '<span class="cb-si-stock">' + b.stock_name + '</span>' : '') +
                '</a>';
      });
      box.innerHTML = html;
      box.style.display = 'block';
    })
    .catch(function (e) {
      box.innerHTML = '<div class="cb-suggest-empty">请求出错：' + e.message + '</div>';
      box.style.display = 'block';
    });
}

// 点击空白处关闭所有搜索下拉
document.addEventListener('click', function (e) {
  if (e.target.closest('.cb-suggest') || e.target.closest('.nav-search') || e.target.closest('.search-box')) {
    return;
  }
  document.querySelectorAll('.cb-suggest').forEach(function (bx) { bx.style.display = 'none'; });
});
