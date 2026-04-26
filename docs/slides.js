const slides = Array.from(document.querySelectorAll('.slide'));
const dotsEl = document.getElementById('dots');
let current = 0;

slides.forEach((slide, i) => {
  const counter = slide.querySelector('.slide-counter');
  if (counter) counter.textContent = `${i + 1} / ${slides.length}`;
  const d = document.createElement('div');
  d.className = 'dot' + (i === 0 ? ' active' : '');
  d.addEventListener('click', () => goTo(i));
  dotsEl.appendChild(d);
});

function goTo(n) {
  slides[current].classList.remove('active');
  dotsEl.children[current].classList.remove('active');
  current = Math.max(0, Math.min(n, slides.length - 1));
  slides[current].classList.add('active');
  dotsEl.children[current].classList.add('active');
  document.getElementById('nav-prev').style.opacity = current === 0 ? '0.3' : '1';
  document.getElementById('nav-next').style.opacity = current === slides.length - 1 ? '0.3' : '1';
}

document.getElementById('nav-prev').addEventListener('click', () => goTo(current - 1));
document.getElementById('nav-next').addEventListener('click', () => goTo(current + 1));

document.addEventListener('keydown', e => {
  if (['ArrowRight', ' ', 'ArrowDown'].includes(e.key)) { e.preventDefault(); goTo(current + 1); }
  if (['ArrowLeft', 'ArrowUp'].includes(e.key)) { e.preventDefault(); goTo(current - 1); }
});

let touchStartX = 0;
document.addEventListener('touchstart', e => { touchStartX = e.touches[0].clientX; }, { passive: true });
document.addEventListener('touchend', e => {
  const dx = e.changedTouches[0].clientX - touchStartX;
  if (Math.abs(dx) > 50) goTo(dx < 0 ? current + 1 : current - 1);
});

goTo(0);
