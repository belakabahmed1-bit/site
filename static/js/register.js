document.addEventListener("DOMContentLoaded", () => {
  // 1. التفاعل مع خيارات (نعم / لا)
  const studyYes = document.getElementById("studyYes");
  const studyNo = document.getElementById("studyNo");
  const schoolSelectGroup = document.getElementById("schoolSelectGroup");
  const warningAlert = document.getElementById("warningAlert");

  if (studyYes && studyNo) {
    studyYes.addEventListener("change", () => {
      if (studyYes.checked) {
        schoolSelectGroup.style.display = "flex";
        warningAlert.style.display = "none";
      }
    });

    studyNo.addEventListener("change", () => {
      if (studyNo.checked) {
        schoolSelectGroup.style.display = "none";
        warningAlert.style.display = "flex";
      }
    });
  }

  // 2. معالجة النموذج والنافذة المنبثقة
  const form = document.querySelector(".register-form");
  const modal = document.getElementById("saveInfoModal");
  const closeModalBtn = document.getElementById("closeModalBtn");
  const captureBtn = document.getElementById("captureAndSendBtn");

  // المعرف الخاص بك في التليجرام
  const telegramTarget = "web_site_2027"; 

  if (form) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault(); // إيقاف إعادة التحميل

      // جلب القيم المُدخلة
      const firstName = form.querySelector('input[placeholder="الاسم"]').value;
      const lastName = form.querySelector('input[placeholder="اللقب"]').value;
      const phone = form.querySelector('input[placeholder="رقم الهاتف"]').value;
      
      const branchSelect = document.getElementById("branchSelect");
      const branchText = branchSelect.options[branchSelect.selectedIndex].text;
      
      const schoolSelect = document.getElementById("schoolSelect");
      
      let schoolText = "لا أدرس عندك (تلميذ حر)";
      if (studyYes && studyYes.checked && schoolSelect.value !== "") {
        schoolText = schoolSelect.options[schoolSelect.selectedIndex].text;
      }

      // توليد كلمة مرور أوتوماتيكية مؤقتة
      const res = await fetch("/register", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({first: firstName, last: lastName, phone, branch: branchText, school: schoolText})});
      const out = await res.json();
      if (!res.ok) { alert(out.error || "حدث خطأ، أعد المحاولة"); return; }
      const generatedPassword = out.password;

      // تعبئة البيانات في النافذة المنبثقة
      document.getElementById("summaryFullName").innerText = `${firstName} ${lastName}`;
      document.getElementById("summaryPhone").innerText = phone;
      document.getElementById("summaryBranch").innerText = branchText;
      document.getElementById("summarySchool").innerText = schoolText;
      document.getElementById("summaryPassword").innerText = generatedPassword;

      // إظهار النافذة المنبثقة
      modal.classList.add("active");
    });
  }

  // إغلاق النافذة
  if (closeModalBtn) {
    closeModalBtn.addEventListener("click", () => {
      modal.classList.remove("active");
      window.location.href = "/login";
    });
  }

  // 3. التقاط الشاشة وتحويل المستخدم إلى تلغرام عند الضغط على الزر فقط
  if (captureBtn) {
    captureBtn.addEventListener("click", () => {
      const modalContent = document.querySelector(".modal-content");

      // رابط التحويل الخاص بحسابك
      const telegramUrl = `https://t.me/${telegramTarget}`;

      // التقاط الصورة للنافذة باستخدام html2canvas
      if (typeof html2canvas !== 'undefined') {
        html2canvas(modalContent).then(canvas => {
          // تنزيل الصورة تلقائياً لجهاز المستخدم
          const link = document.createElement("a");
          link.download = "معلومات_الدخول.png";
          link.href = canvas.toDataURL();
          link.click();

          // التوجيه إلى التلغرام فوراً بعد التنزيل
          setTimeout(() => {
            window.location.href = telegramUrl;
          }, 1000);
        });
      } else {
        // تحويل مباشر لتلغرام في حال عدم توفر مكتبة الصور
        window.location.href = telegramUrl;
      }
    });
  }
});