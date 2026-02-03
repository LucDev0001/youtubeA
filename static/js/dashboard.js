// Variáveis Globais
let currentUser = null;
let userToken = null;
let isRunning = false;
let currentNextPageToken = null;
let currentChannelFilter = null;
let liveFilterActive = false;

// --- Autenticação e Inicialização ---
auth.onAuthStateChanged(async (user) => {
  if (user) {
    currentUser = user;
    userToken = await user.getIdToken();
    checkUserStatus();
    // Carrega vídeos iniciais
    loadRecentVideos();
  } else {
    // Se não estiver logado, manda pro login
    window.location.href = "/login";
  }
});

async function checkUserStatus() {
  const doc = await db.collection("users").doc(currentUser.uid).get();
  if (doc.exists) {
    const data = doc.data();

    // Atualiza UI do Plano
    const planName = data.plan === "pro" ? "PRO 💎" : "Gratuito";
    const credits = data.credits || 0;
    const planEl = document.getElementById("planStatus");
    if (planEl) planEl.innerText = `${planName} (${credits} créditos)`;

    if (data.plan !== "pro") {
      const upBtn = document.getElementById("upgradeBtn");
      if (upBtn) upBtn.classList.remove("hidden");
    }

    // Atualiza UI do YouTube
    if (data.youtube_connected) {
      document.getElementById("ytConnectionStatus").classList.add("hidden");
      document.getElementById("ytConnected").classList.remove("hidden");
      document.getElementById("botArea").classList.remove("hidden");
    } else {
      document.getElementById("ytConnectionStatus").classList.remove("hidden");
      document.getElementById("ytConnected").classList.add("hidden");
      document.getElementById("botArea").classList.add("hidden");
    }
  }
}

function connectYoutube() {
  window.location.href = `/connect_youtube?uid=${currentUser.uid}`;
}

function logout() {
  auth.signOut().then(() => {
    window.location.href = "/";
  });
}

// --- Lógica do Bot ---

document
  .getElementById("botForm")
  ?.addEventListener("submit", async function (e) {
    e.preventDefault();
    const btn = document.getElementById("btnSubmit");

    if (isRunning) {
      isRunning = false;
      btn.innerText = "Parando...";
      btn.disabled = true;
      return;
    }

    const resultDiv = document.getElementById("result");
    const formData = new FormData(this);
    const isAuto = document.getElementById("autoMode").checked;
    const count = parseInt(document.getElementById("repeatCount").value) || 1;
    const interval =
      parseInt(document.getElementById("repeatInterval").value) || 60;

    if (isAuto && interval < 5) {
      alert("Por segurança, o intervalo mínimo é de 5 segundos.");
      return;
    }

    isRunning = true;
    const delay = (ms) => new Promise((res) => setTimeout(res, ms));
    let total = isAuto ? count : 1;

    if (isAuto) {
      btn.innerText = "Parar Automação";
      btn.classList.add("bg-gray-600");
      btn.classList.remove("bg-yt-red");
    } else {
      btn.disabled = true;
      btn.innerText = "Enviando...";
    }

    for (let i = 1; i <= total; i++) {
      if (!isRunning) break;

      if (isAuto) {
        resultDiv.style.display = "block";
        resultDiv.className =
          "mt-4 text-center text-sm p-2.5 rounded bg-gray-800 text-white";
        resultDiv.innerText = `Enviando mensagem ${i} de ${total}...`;
      }

      try {
        const response = await fetch("/send", {
          method: "POST",
          headers: { Authorization: `Bearer ${userToken}` },
          body: formData,
        });
        const data = await response.json();

        resultDiv.innerText = isAuto
          ? `[${i}/${total}] ${data.message}`
          : data.message;
        const bgClass =
          data.status === "success"
            ? "bg-green-900 text-green-100"
            : "bg-red-900 text-red-100";
        resultDiv.className = `mt-4 text-center text-sm p-2.5 rounded ${bgClass}`;
        resultDiv.style.display = "block";
      } catch (err) {
        resultDiv.innerText = `Erro de conexão na tentativa ${i}.`;
        resultDiv.className =
          "mt-4 text-center text-sm p-2.5 rounded bg-red-900 text-red-100";
        resultDiv.style.display = "block";
      }

      if (i < total && isRunning) {
        let remaining = interval;
        while (remaining > 0 && isRunning) {
          btn.innerText = `Parar (Próximo em ${remaining}s)`;
          await delay(1000);
          remaining--;
        }
        btn.innerText = "Parar Automação";
      }
    }

    isRunning = false;
    btn.disabled = false;
    btn.innerText = "Enviar";
    btn.classList.remove("bg-gray-600");
    btn.classList.add("bg-yt-red");
  });

// --- UI Helpers ---

// Preview do Vídeo
document
  .getElementById("video_id")
  ?.addEventListener("blur", async function () {
    const val = this.value.trim();
    if (val.length < 5) return;
    try {
      const res = await fetch("/get_video_info", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${userToken}`,
        },
        body: JSON.stringify({ video_id: val }),
      });
      const data = await res.json();
      if (data.status === "success") {
        document.getElementById("thumb").src = data.thumbnail;
        document.getElementById("vidTitle").innerText = data.title;
        document.getElementById("vidChannel").innerText = data.channel;
        document.getElementById("videoPreview").classList.remove("hidden");
      } else {
        document.getElementById("videoPreview").classList.add("hidden");
      }
    } catch (e) {
      console.error(e);
    }
  });

// Carregar Vídeos
async function loadRecentVideos(pageToken = "", append = false) {
  const container = document.getElementById("recentVideos");
  const loadMoreBtn = document.getElementById("loadMoreBtn");
  if (!container) return;

  if (!append) {
    container.innerHTML =
      '<p class="text-gray-500 text-sm">Carregando vídeos...</p>';
    loadMoreBtn.style.display = "none";
  }

  try {
    let url = `/get_recent_videos?pageToken=${pageToken}`;
    if (currentChannelFilter) url += `&channelId=${currentChannelFilter}`;
    if (liveFilterActive) url += `&liveOnly=true`;

    const res = await fetch(url, {
      headers: { Authorization: `Bearer ${userToken}` },
    });
    const data = await res.json();

    currentNextPageToken = data.nextPageToken;
    loadMoreBtn.style.display = currentNextPageToken ? "inline-block" : "none";

    if (data.status === "success" && data.videos.length > 0) {
      if (!append) container.innerHTML = "";
      data.videos.forEach((vid) => {
        let badgeClass = vid.type.includes("LIVE")
          ? "bg-red-700"
          : vid.type.includes("BREVE")
            ? "bg-orange-600"
            : "bg-teal-700";
        const div = document.createElement("div");
        div.className =
          "bg-yt-card dark:bg-light-card group relative cursor-pointer hover:scale-105 transition-transform duration-200";
        div.onclick = () => {
          const input = document.getElementById("video_id");
          input.value = vid.id;
          input.focus();
          input.blur();
          window.scrollTo({ top: 0, behavior: "smooth" });
        };
        div.innerHTML = `
            <div class="relative">
                <img src="${vid.thumbnail}" class="w-full h-32 object-cover rounded-lg">
                <span class="absolute top-2 right-2 text-[10px] px-2 py-0.5 rounded font-bold text-white shadow-md ${badgeClass}">${vid.type}</span>
            </div>
            <div class="py-2">
                <p class="text-sm font-medium text-yt-text dark:text-light-text line-clamp-2 leading-tight">${vid.title}</p>
                <span class="text-xs text-gray-500 block mt-1">${vid.channel}</span>
                ${vid.viewers ? `<span class="text-xs text-red-500 font-bold block mt-1">👥 ${vid.viewers}</span>` : ""}
            </div>
        `;
        container.appendChild(div);
      });
    } else if (!append) {
      container.innerHTML =
        '<p class="text-gray-500 text-sm">Nenhum vídeo encontrado.</p>';
    }
  } catch (e) {
    console.error(e);
    container.innerHTML =
      '<p class="text-red-500 text-sm">Erro ao carregar vídeos.</p>';
  }
}

// Sidebar Toggle
const menuToggle = document.getElementById("menuToggle");
const sidebar = document.getElementById("sidebar");
const mainContent = document.getElementById("mainContent");

if (menuToggle) {
  menuToggle.addEventListener("click", () => {
    sidebar.classList.toggle("-translate-x-full");
    // Ajuste para desktop se necessário, mas o Tailwind lida bem com classes responsivas
  });
}

// Theme Toggle
const themeToggle = document.getElementById("themeToggle");
const html = document.documentElement;
const savedTheme = localStorage.getItem("theme") || "dark";
if (savedTheme === "dark") html.classList.add("dark");
else html.classList.remove("dark");

if (themeToggle) {
  themeToggle.addEventListener("click", () => {
    html.classList.toggle("dark");
    localStorage.setItem(
      "theme",
      html.classList.contains("dark") ? "dark" : "light",
    );
  });
}
