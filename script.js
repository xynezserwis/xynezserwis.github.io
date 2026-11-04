/* Wklej tu adres swojego formularza z Formspree */
var ENDPOINT = 'https://formspree.io/f/YOUR_FORM_ID';

var fm = document.getElementById('f'),
    btn = fm.querySelector('.send'),
    ok = document.getElementById('ok'),
    er = document.getElementById('err');

fm.addEventListener('submit', function (ev) {
  ev.preventDefault();
  ok.hidden = true; er.hidden = true; btn.disabled = true;
  fetch(ENDPOINT, { method: 'POST', body: new FormData(fm), headers: { 'Accept': 'application/json' } })
    .then(function (r) { if (r.ok) { fm.reset(); ok.hidden = false; } else { er.hidden = false; } })
    .catch(function () { er.hidden = false; })
    .then(function () { btn.disabled = false; });
});

var els = document.querySelectorAll('.sc');
if (!('IntersectionObserver' in window)) {
  els.forEach(function (x) { x.classList.add('in'); });
} else {
  var io = new IntersectionObserver(function (l) {
    l.forEach(function (i) { if (i.isIntersecting) { i.target.classList.add('in'); io.unobserve(i.target); } });
  }, { threshold: .1 });
  els.forEach(function (x) { io.observe(x); });
}
