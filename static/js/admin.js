const ADMIN_UID = "BDZUzrq5kSSG5TMO3s2LI15gWEu2";

auth.onAuthStateChanged(async (user) => {
  if (user) {
    if (user.uid !== ADMIN_UID) {
      alert("Acesso negado. Você não é administrador.");
      window.location.href = "/app";
      return;
    }
    loadAdminData(user);
  } else {
    window.location.href = "/login";
  }
});

async function loadAdminData(user) {
  try {
    const token = await user.getIdToken();
    const response = await fetch("/api/admin/data", {
      headers: { Authorization: `Bearer ${token}` },
    });

    if (!response.ok) throw new Error("Falha ao carregar dados");

    const data = await response.json();

    // Preencher Preço
    document.getElementById("priceInput").value = data.price.toFixed(2);

    // Preencher Tabela
    const tbody = document.getElementById("usersTableBody");
    tbody.innerHTML = "";
    document.getElementById("userCount").innerText = data.users.length;

    data.users.forEach((u) => {
      const row = document.createElement("tr");
      row.className = "border-b border-gray-800 hover:bg-gray-800/50";

      const planBadge =
        u.plan === "pro"
          ? '<span class="bg-green-900 text-green-100 px-2 py-0.5 rounded text-xs">PRO</span>'
          : '<span class="bg-gray-700 text-gray-300 px-2 py-0.5 rounded text-xs">Free</span>';

      row.innerHTML = `
                <td class="p-3">
                    <div class="font-bold">${u.email}</div>
                    <div class="text-xs text-gray-500">${u.uid}</div>
                </td>
                <td class="p-3">${planBadge}</td>
                <td class="p-3">${u.credits}</td>
                <td class="p-3">${u.daily_count}</td>
                <td class="p-3 text-gray-400">${new Date(u.created_at).toLocaleDateString()}</td>
            `;
      tbody.appendChild(row);
    });

    document.getElementById("loading").classList.add("hidden");
    document.getElementById("adminContent").classList.remove("hidden");
  } catch (e) {
    alert("Erro: " + e.message);
  }
}

async function updatePrice() {
  const newPrice = document.getElementById("priceInput").value;
  const token = await auth.currentUser.getIdToken();

  const response = await fetch("/api/admin/price", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ price: newPrice }),
  });

  const res = await response.json();
  if (res.status === "success") alert("Preço atualizado com sucesso!");
  else alert("Erro ao atualizar.");
}

function logout() {
  auth.signOut().then(() => (window.location.href = "/"));
}
