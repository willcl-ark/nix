"use strict";

(async () => {
  const root = document.querySelector("[data-feedback-url]");
  if (!root) return;
  const forms = [...root.querySelectorAll(".finding-feedback")];
  if (!forms.length) return;
  const endpoint = root.dataset.feedbackUrl;
  const saved = new Map();
  let token;
  let storageError = false;
  try {
    token = localStorage.getItem("ralph-feedback-token");
    if (!/^[a-f0-9]{64}$/.test(token || "")) {
      token = [...crypto.getRandomValues(new Uint8Array(32))]
        .map(byte => byte.toString(16).padStart(2, "0")).join("");
      localStorage.setItem("ralph-feedback-token", token);
    }
  } catch (_) {
    storageError = true;
    token = null;
  }

  async function request(path, body) {
    const headers = token ? {"X-Feedback-Token": token} : {};
    const options = {headers, cache: "no-store", credentials: "omit"};
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      options.method = "POST";
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    if (!response.ok) {
      if (response.status === 429) throw new Error("Please wait a moment before saving again.");
      throw new Error("Feedback could not be saved or loaded. Please try again.");
    }
    return response.json();
  }

  function update(data, editedForm = null) {
    for (const form of forms) {
      const value = data.findings[form.dataset.findingId];
      if (!value) continue;
      for (const count of form.querySelectorAll("[data-count]")) {
        count.textContent = value[count.dataset.count];
      }
      if (editedForm && form !== editedForm) continue;
      const mine = value.mine;
      saved.set(form, mine);
      for (const button of form.querySelectorAll("[data-vote]")) {
        button.setAttribute("aria-pressed", String(mine?.vote === Number(button.dataset.vote)));
      }
      form.querySelector(".feedback-selection").textContent = mine
        ? (mine.vote === 1 ? "You found this useful" : "You found this unhelpful") : "";
      form.elements.comment.value = mine?.comment || "";
      form.querySelector(".feedback-remove").hidden = !mine;
      form.querySelector(".feedback-explanation").open = Boolean(mine?.comment);
    }
  }

  async function save(form, vote) {
    if (vote !== null && vote !== 1 && vote !== -1) {
      form.querySelector(".feedback-status").textContent = "Choose useful or not useful first.";
      return;
    }
    for (const item of forms) item.querySelector("fieldset").disabled = true;
    const status = form.querySelector(".feedback-status");
    status.textContent = "Saving…";
    try {
      const data = await request(endpoint + "/" + encodeURIComponent(form.dataset.findingId), {
        vote, comment: vote === null ? "" : form.elements.comment.value,
      });
      update(data, form);
      status.textContent = vote === null ? "Feedback removed." : "Feedback saved. Thank you.";
    } catch (error) {
      status.textContent = error.message;
    } finally {
      for (const item of forms) item.querySelector("fieldset").disabled = false;
    }
  }

  for (const form of forms) {
    form.hidden = false;
    form.querySelector(".feedback-status").textContent = "Loading feedback…";
    for (const button of form.querySelectorAll("[data-vote]")) {
      button.addEventListener("click", () => {
        const vote = Number(button.dataset.vote);
        save(form, saved.get(form)?.vote === vote ? null : vote);
      });
    }
    form.addEventListener("submit", event => {
      event.preventDefault();
      save(form, saved.get(form)?.vote);
    });
    form.querySelector(".feedback-remove").addEventListener("click", () => save(form, null));
  }
  try {
    update(await request(endpoint));
    for (const form of forms) {
      form.querySelector("fieldset").disabled = storageError;
      form.querySelector(".feedback-status").textContent = storageError
        ? "Enable browser storage to vote and edit your feedback." : "";
    }
  } catch (_) {
    for (const form of forms) {
      form.querySelector(".feedback-status").textContent = "Feedback is unavailable. Reload the page to try again.";
    }
  }
})();
