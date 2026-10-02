// Copie le texte d'un champ dans le presse-papiers (avec repli pour les contextes non sécurisés)
async function copyFrom(id, btn) {
  const el = document.getElementById(id);
  try {
    await navigator.clipboard.writeText(el.value);
  } catch (e) {
    el.select();
    document.execCommand("copy");
  }
  const old = btn.textContent;
  btn.textContent = "Copié ✓";
  setTimeout(() => (btn.textContent = old), 1500);
}

// Rafraîchit la page pendant qu'un traitement est en cours
if (document.body.dataset.autorefresh === "1") {
  setTimeout(() => location.reload(), 8000);
}
