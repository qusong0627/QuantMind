// QuantMind Documentation Scripts

document.addEventListener('DOMContentLoaded', function() {
  // Mobile menu toggle
  const mobileMenuBtn = document.getElementById('mobile-menu-btn');
  const sidebar = document.querySelector('.sidebar');
  const overlay = document.querySelector('.overlay');

  if (mobileMenuBtn) {
    mobileMenuBtn.addEventListener('click', function() {
      sidebar.classList.toggle('open');
      overlay.classList.toggle('open');
    });
  }

  if (overlay) {
    overlay.addEventListener('click', function() {
      sidebar.classList.remove('open');
      overlay.classList.remove('open');
    });
  }

  // Highlight current nav link
  const currentPath = window.location.pathname;
  const navLinks = document.querySelectorAll('.nav-link');

  navLinks.forEach(link => {
    const href = link.getAttribute('href');
    if (href === currentPath || (currentPath.endsWith('/') && href === currentPath.slice(0, -1)) ||
        (currentPath === '/' && href === 'index.html') ||
        (currentPath === '/index.html' && href === 'index.html')) {
      link.classList.add('active');
    }
  });

  // Smooth scroll for anchor links
  document.querySelectorAll('a[href^="#"]').forEach(anchor => {
    anchor.addEventListener('click', function(e) {
      e.preventDefault();
      const target = document.querySelector(this.getAttribute('href'));
      if (target) {
        target.scrollIntoView({
          behavior: 'smooth',
          block: 'start'
        });
      }
    });
  });

  // Copy code button
  document.querySelectorAll('.code-block').forEach(block => {
    const copyBtn = document.createElement('button');
    copyBtn.className = 'btn btn-secondary';
    copyBtn.style.cssText = 'position: absolute; top: 8px; right: 8px; padding: 4px 8px; font-size: 12px;';
    copyBtn.textContent = 'Copy';

    copyBtn.addEventListener('click', function() {
      const code = block.querySelector('code');
      navigator.clipboard.writeText(code.textContent).then(() => {
        copyBtn.textContent = 'Copied!';
        setTimeout(() => {
          copyBtn.textContent = 'Copy';
        }, 2000);
      });
    });

    block.style.position = 'relative';
    block.appendChild(copyBtn);
  });
});
