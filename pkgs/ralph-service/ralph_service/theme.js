(()=>{
const themeToggle = document.querySelector('[data-theme-toggle]');
if (themeToggle) {
  function setTheme(theme) {
    document.documentElement.dataset.theme = theme;
    themeToggle.textContent = theme === 'dark' ? 'Light theme' : 'Dark theme';
  }
  let theme = 'dark';
  try { theme = localStorage.getItem('ralph-theme') === 'light' ? 'light' : 'dark'; } catch {}
  setTheme(theme);
  themeToggle.hidden = false;
  themeToggle.addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    setTheme(next);
    try { localStorage.setItem('ralph-theme', next); } catch {}
  });
}
})();
