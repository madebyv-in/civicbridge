(() => {
  const log = document.getElementById('log');
  const promptInput = document.getElementById('prompt');
  const sendBtn = document.getElementById('send');
  const langSelect = document.getElementById('lang');
  const recordBtn = document.getElementById('recordBtn');
  const recordStatus = document.getElementById('recordStatus');
  const chipsContainer = document.getElementById('chips');

  let ws = null;
  let mediaRecorder = null;
  let mediaStream = null;
  let audioChunks = [];
  let isRecording = false;

  function formatText(text) {
    if (!text) return '';
    
    text = text.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
    text = text.replace(/\n/g, '<br>');
    
    return text;
  }

  function addMessage(role, text) {
    const messageDiv = document.createElement('div');
    messageDiv.className = `message ${role}`;
    
    const avatar = document.createElement('div');
    avatar.className = 'message-avatar';
    avatar.textContent = role === 'user' ? 'U' : 'CB';
    
    const content = document.createElement('div');
    content.className = 'message-content';
    content.innerHTML = formatText(text);
    
    messageDiv.appendChild(avatar);
    messageDiv.appendChild(content);
    log.appendChild(messageDiv);
    log.scrollTop = log.scrollHeight;

    if (role === 'assistant' && typeof text === 'string' && text.trim()) {
      try { speak(text); } catch (e) {}
    }
  }

  function addSystemMessage(text) {
    const systemDiv = document.createElement('div');
    systemDiv.className = 'system-message';
    systemDiv.textContent = text;
    log.appendChild(systemDiv);
    log.scrollTop = log.scrollHeight;
  }

  function speak(text, lang = null) {
    if (!('speechSynthesis' in window)) return;
    const utter = new SpeechSynthesisUtterance(text);
    const isChinese = /[\u4e00-\u9fff]/.test(text);
    const targetLang = lang === 'zh' || isChinese ? 'zh-CN' : (lang || 'en-US');
    const voices = window.speechSynthesis.getVoices();
    let chosen = voices.find(v => v.lang && v.lang.toLowerCase().startsWith(targetLang.toLowerCase()));
    if (!chosen && isChinese) chosen = voices.find(v => v.lang && v.lang.toLowerCase().startsWith('zh'));
    if (!chosen) chosen = voices[0];
    if (chosen) utter.voice = chosen;
    utter.lang = chosen ? chosen.lang : targetLang;
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(utter);
  }

  function clearLog() {
    log.innerHTML = '';
  }

  function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${protocol}//${window.location.host}/ws`);
    
    ws.onopen = () => {
      clearLog();
      addSystemMessage('Connected! How can I help you today?');
    };
    
    ws.onmessage = (event) => {
      let payload = null;
      try {
        payload = JSON.parse(event.data);
      } catch (e) {
        addMessage('assistant', event.data);
        return;
      }

      if (payload && typeof payload === 'object') {
        if (payload.response) {
          addMessage('assistant', payload.response);
        } else if (payload.message && !payload.action) {
          addMessage('assistant', payload.message);
        } else if (payload.action === 'open_url' && payload.url) {
          addSystemMessage(`Opening: ${payload.url}`);
          if (payload.message) {
            addMessage('assistant', payload.message);
          }
          window.open(payload.url, '_blank');
        } else if (payload.action === 'error') {
          addSystemMessage(`Error: ${payload.message}`);
        }
      } else {
        addMessage('assistant', event.data);
      }
    };
    
    ws.onerror = () => {
      addSystemMessage('Connection error. Retrying...');
    };
    
    ws.onclose = () => {
      addSystemMessage('Disconnected. Reconnecting in 2 seconds...');
      setTimeout(connectWebSocket, 2000);
    };
  }

  function sendMessage() {
    const text = promptInput.value.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
    
    addMessage('user', text);
    
    const lang = langSelect.value;
    const verbosity = (document.getElementById('verbosity') && document.getElementById('verbosity').value) || 'verbose';
    ws.send(JSON.stringify({ action: 'message', text, lang, verbosity }));
    
    promptInput.value = '';
  }

  sendBtn.addEventListener('click', sendMessage);

  promptInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') {
      sendMessage();
    }
  });

  // wire chips to inject quick texts into the prompt and optionally send
  if (chipsContainer) {
    chipsContainer.addEventListener('click', (e) => {
      const btn = e.target.closest('button.chip');
      if (!btn) return;
      const text = btn.textContent.trim();
      if (!text) return;
      // inject into prompt and auto-send
      promptInput.value = text;
      // call existing sendMessage so verbosity and other metadata are included
      sendMessage();
    });
  }

  async function startRecording() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      addSystemMessage('MediaRecorder not supported in this browser.');
      return false;
    }
    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      addSystemMessage('Microphone access denied or unavailable: ' + (err && err.message ? err.message : err));
      return false;
    }
    try {
      mediaRecorder = new MediaRecorder(mediaStream);
    } catch (err) {
      addSystemMessage('Failed to create MediaRecorder: ' + (err && err.message ? err.message : err));
      return false;
    }
    audioChunks = [];
    mediaRecorder.ondataavailable = (e) => { 
      if (e.data && e.data.size) audioChunks.push(e.data); 
    };
    mediaRecorder.onstart = () => { 
      isRecording = true; 
      recordStatus.textContent = 'Recording...'; 
      // keep label consistent with new UI naming
      recordBtn.textContent = 'Voice Mode — Stop'; 
      recordBtn.classList.add('recording');
      addSystemMessage('Recording started'); 
    };
    mediaRecorder.onstop = async () => {
      isRecording = false;
      recordStatus.textContent = 'Processing...';
      recordBtn.textContent = 'Voice Mode';
      recordBtn.classList.remove('recording');
      const blob = new Blob(audioChunks, { type: 'audio/webm' });
      
      const fd = new FormData();
      fd.append('file', blob, 'recording.webm');
      // Default to English when lang isn't set; only use Chinese when
      // user explicitly selected Chinese (value starts with 'zh').
      fd.append('lang', langSelect.value || 'en');
      // Include the most recent assistant message (if any) so the server
      // can treat this transcription as a reply to that assistant prompt
      // and perform auto-fill actions when appropriate.
      try {
        const assistantMessages = document.querySelectorAll('.message.assistant .message-content');
        if (assistantMessages && assistantMessages.length) {
          const last = assistantMessages[assistantMessages.length - 1];
          const txt = last.textContent && last.textContent.trim();
          if (txt) fd.append('prev_assistant', txt);
        }
      } catch (e) {
        // ignore DOM errors
      }
      
      try {
        const res = await fetch('/audio', { method: 'POST', body: fd });
        if (!res.ok) {
          const err = await res.text();
          addSystemMessage('Transcription error: ' + err);
        } else {
          const data = await res.json();
          if (data.transcription) {
            addMessage('user', `${data.transcription}`);
          }
          // Show which language the server received and used for transcription
          if (data.transcribe_lang || data.lang_received) {
            const rec = data.lang_received || 'unknown';
            const used = data.transcribe_lang || 'unknown';
            addSystemMessage(`Transcribed (received lang=${rec}, used=${used})`);
          }
          if (data.response) {
            if (typeof data.response === 'object' && data.response.response) {
              addMessage('assistant', data.response.response);
            } else if (typeof data.response === 'string') {
              addMessage('assistant', data.response);
            }
          }
        }
      } catch (err) {
        addSystemMessage('Upload failed: ' + (err && err.message ? err.message : err));
      }
      
      try { mediaStream.getTracks().forEach(t => t.stop()); } catch (e) {}
      mediaStream = null;
      mediaRecorder = null;
      audioChunks = [];
      recordStatus.textContent = '';
    };
    mediaRecorder.start();
    return true;
  }

  recordBtn.addEventListener('click', async () => {
    try {
      if (isRecording && mediaRecorder) {
        mediaRecorder.stop();
      } else {
        await startRecording();
      }
      } catch (err) {
      addSystemMessage('Recording error: ' + (err && err.message ? err.message : err));
      recordStatus.textContent = '';
      // ensure UI shows the renamed primary state
      recordBtn.textContent = 'Voice Mode';
      recordBtn.classList.remove('recording');
      try { if (mediaStream) mediaStream.getTracks().forEach(t => t.stop()); } catch (e) {}
      mediaStream = null; 
      mediaRecorder = null; 
      audioChunks = [];
      isRecording = false;
    }
  });

  // upload audio input removed from UI — no-op

  connectWebSocket();
})();