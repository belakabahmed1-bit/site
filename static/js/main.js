document.addEventListener("DOMContentLoaded", () => {
  const menuBtn = document.querySelector(".menu-btn"); // زر القائمة العلوي
  const sideMenu = document.getElementById("sideMenu");
  const closeMenuBtn = document.getElementById("closeMenuBtn");

  // فتح القائمة
  if (menuBtn) {
    menuBtn.addEventListener("click", () => {
      sideMenu.classList.add("active");
    });
  }

  // إغلاق القائمة عند الضغط على X
  if (closeMenuBtn) {
    closeMenuBtn.addEventListener("click", () => {
      sideMenu.classList.remove("active");
    });
  }
});