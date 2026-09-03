import { atom, Button, cn, haptic, host, icons, Tip, useComposerVoiceController, useValue } from '@hermes/plugin-sdk'
import { Fragment, jsx, jsxs } from 'react/jsx-runtime'

const TIMEOUT_MS = 30_000
const RENDER_INSTRUCTIONS =
  "Speak the sole user input verbatim. Add, remove, or paraphrase nothing. Do not preface or explain."
const IDLE = Object.freeze({ phase: 'idle', status: 'Realtime Talk', muted: false, error: '' })
const $talk = atom(IDLE)

let pluginContext = null
let transport = null
let startGeneration = 0
let activeController = null
const releasedControllers = new WeakSet()

function errorText(error) {
  if (error instanceof Error && error.message) return error.message
  if (error && typeof error === 'object' && typeof error.detail === 'string') return error.detail
  return String(error || 'Realtime Talk failed.')
}

function releaseController(controller = activeController) {
  if (!controller || releasedControllers.has(controller)) return
  if (activeController === controller) activeController = null
  releasedControllers.add(controller)
  controller.release?.()
}

function clearTransport(instance) {
  if (transport === instance) transport = null
}

function cleanupStaleStart(instance, controller) {
  instance.stop()
  clearTransport(instance)
  releaseController(controller)
}

function stopTalk() {
  startGeneration += 1
  transport?.stop()
  transport = null
  releaseController()
  $talk.set(IDLE)
}

async function startTalk(voiceController) {
  if (transport || $talk.get().phase === 'starting') {
    stopTalk()
    return
  }

  const generation = ++startGeneration
  $talk.set({ ...IDLE, phase: 'starting', status: 'Connecting…' })
  let lease = null
  try {
    if (!pluginContext) throw new Error('Realtime Talk plugin is not ready.')
    if (typeof RTCPeerConnection === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
      throw new Error('Realtime audio is not supported by this Desktop build.')
    }

    lease = await voiceController?.acquire()
    if (!lease) throw new Error('The microphone is unavailable.')
    if (generation !== startGeneration) {
      releaseController(lease)
      return
    }
    activeController = lease

    const session = await pluginContext.rest('/session', {
      method: 'POST',
      body: { mode: 'desktop-relay' },
      timeoutMs: TIMEOUT_MS
    })
    if (generation !== startGeneration) {
      releaseController(lease)
      return
    }
    if (!session?.clientSecret || !session?.offerUrl || session.mode !== 'desktop-relay') {
      throw new Error('The backend does not support canonical Desktop Realtime Talk.')
    }

    const next = new DesktopTalkTransport(session, voiceController, {
      onState: status => $talk.set({ ...$talk.get(), phase: 'active', status, error: '' }),
      onError: message => {
        host.notifyError(new Error(message), 'Realtime Talk stopped')
        stopTalk()
      }
    })
    transport = next
    await next.start()
    if (generation !== startGeneration) {
      cleanupStaleStart(next, lease)
      return
    }
    $talk.set({ ...$talk.get(), phase: 'active', status: 'Listening…', error: '' })
  } catch (error) {
    if (generation !== startGeneration) {
      releaseController(lease)
      return
    }
    transport?.stop()
    transport = null
    releaseController(lease)
    $talk.set({ ...IDLE, phase: 'error', status: 'Realtime unavailable', error: errorText(error) })
    host.notifyError(error, 'Realtime Talk unavailable')
  }
}

function toggleMute() {
  if (!transport) return
  const muted = !transport.muted
  transport.setMuted(muted)
  $talk.set({ ...$talk.get(), muted, status: muted ? 'Muted' : 'Listening…' })
}

class DesktopTalkTransport {
  constructor(session, controller, callbacks) {
    this.session = session
    this.controller = controller
    this.callbacks = callbacks
    this.peer = null
    this.channel = null
    this.media = null
    this.audio = null
    this.offerAbort = null
    this.unsubscribeAssistant = null
    this.closed = false
    this.muted = false
    this.responseActive = false
    this.playbackActive = false
    this.responseId = null
    this.latestSpeechItemId = null
    this.waitingForAssistant = false
    this.baselineAssistantId = null
    this.lastSpokenAssistantId = null
    this.turnGeneration = 0
    this.pendingText = null
    this.turnPump = null
    this.activeTurnGeneration = null
    this.interruptPromise = null
    this.channelOpenPromise = null
  }

  async start() {
    const peer = new RTCPeerConnection()
    this.peer = peer
    const audio = document.createElement('audio')
    audio.autoplay = true
    audio.style.display = 'none'
    document.body.appendChild(audio)
    this.audio = audio
    peer.addEventListener('track', event => {
      if (this.audio && event.streams[0]) this.audio.srcObject = event.streams[0]
    })
    peer.addEventListener('connectionstatechange', () => {
      if (!this.closed && ['failed', 'closed'].includes(peer.connectionState)) {
        this.callbacks.onError('Realtime connection closed.')
      }
    })

    const media = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
    })
    if (this.closed) {
      media.getTracks().forEach(track => track.stop())
      return
    }
    this.media = media
    media.getAudioTracks().forEach(track => peer.addTrack(track, media))

    const channel = peer.createDataChannel('oai-events')
    this.channel = channel
    this.channelOpenPromise = new Promise((resolve, reject) => {
      const timer = window.setTimeout(() => reject(new Error('Realtime event channel did not open.')), TIMEOUT_MS)
      channel.addEventListener('open', () => {
        window.clearTimeout(timer)
        this.callbacks.onState('Listening…')
        resolve()
      }, { once: true })
      channel.addEventListener('close', () => {
        window.clearTimeout(timer)
        if (!this.closed) reject(new Error('Realtime event channel closed during setup.'))
      }, { once: true })
    })
    channel.addEventListener('message', event => this.handleEvent(event.data))
    channel.addEventListener('close', () => {
      if (!this.closed) this.callbacks.onError('Realtime event channel closed.')
    })
    this.unsubscribeAssistant = this.controller.subscribeAssistant(response => this.handleAssistantResponse(response))

    const offer = await peer.createOffer()
    await peer.setLocalDescription(offer)
    const answer = await this.postOffer(offer)
    if (this.closed) return
    await peer.setRemoteDescription({ type: 'answer', sdp: answer })
    await this.channelOpenPromise
  }

  async postOffer(offer) {
    const controller = new AbortController()
    this.offerAbort = controller
    const timer = window.setTimeout(() => controller.abort(), TIMEOUT_MS)
    try {
      const response = await fetch(this.session.offerUrl, {
        method: 'POST', body: offer.sdp,
        headers: { Authorization: `Bearer ${this.session.clientSecret}`, 'Content-Type': 'application/sdp' },
        signal: controller.signal
      })
      if (!response.ok) throw new Error(`Realtime WebRTC setup failed (${response.status}).`)
      return response.text()
    } finally {
      window.clearTimeout(timer)
      if (this.offerAbort === controller) this.offerAbort = null
    }
  }

  stop() {
    if (this.closed) return
    this.closed = true
    this.turnGeneration += 1
    this.pendingText = null
    this.offerAbort?.abort()
    this.offerAbort = null
    this.unsubscribeAssistant?.()
    this.unsubscribeAssistant = null
    this.channel?.close()
    this.channel = null
    this.peer?.close()
    this.peer = null
    this.media?.getTracks().forEach(track => track.stop())
    this.media = null
    if (this.audio) {
      this.audio.srcObject = null
      this.audio.remove()
    }
    this.audio = null
  }

  setMuted(muted) {
    this.muted = muted
    this.media?.getAudioTracks().forEach(track => { track.enabled = !muted })
  }

  send(payload) {
    if (this.closed || this.channel?.readyState !== 'open') return false
    this.channel.send(JSON.stringify(payload))
    return true
  }

  handleEvent(data) {
    if (this.closed) return
    let event
    try { event = JSON.parse(String(data)) } catch { return }
    switch (event.type) {
      case 'conversation.item.input_audio_transcription.completed': {
        const text = typeof event.transcript === 'string' ? event.transcript.trim() : ''
        if (event.item_id && event.item_id !== this.latestSpeechItemId) break
        if (text) void this.submitText(text)
        break
      }
      case 'input_audio_buffer.speech_started':
        this.latestSpeechItemId = event.item_id || null
        this.turnGeneration += 1
        this.pendingText = null
        this.waitingForAssistant = false
        this.activeTurnGeneration = null
        this.callbacks.onState('Listening…')
        if (this.responseActive && this.responseId) this.send({ type: 'response.cancel', response_id: this.responseId })
        if (this.playbackActive) {
          this.send({ type: 'output_audio_buffer.clear' })
        }
        if (!this.interruptPromise) {
          this.interruptPromise = Promise.resolve(this.controller.interrupt()).finally(() => { this.interruptPromise = null })
        }
        break
      case 'input_audio_buffer.speech_stopped': this.callbacks.onState('Transcribing…'); break
      case 'response.created':
        this.responseActive = true
        this.responseId = event.response?.id || null
        this.callbacks.onState('Speaking…')
        break
      case 'response.done':
        this.responseActive = false
        this.callbacks.onState('Listening…')
        break
      case 'output_audio_buffer.started': this.playbackActive = true; break
      case 'output_audio_buffer.stopped':
      case 'output_audio_buffer.cleared':
        this.playbackActive = false
        this.responseId = null
        break
      case 'error': {
        const detail = String(event.error?.message || event.error?.code || event.error?.type || '')
        if (!detail.toLowerCase().includes('no active response')) this.callbacks.onError(detail ? `Realtime error: ${detail}` : 'Realtime error.')
        break
      }
      default: break
    }
  }

  submitText(text) {
    const generation = ++this.turnGeneration
    this.pendingText = { generation, text }
    this.turnPump ||= this.drainText().finally(() => { this.turnPump = null })
    return this.turnPump
  }

  async drainText() {
    while (!this.closed && this.pendingText) {
      const turn = this.pendingText
      this.pendingText = null
      await this.submitPendingText(turn)
    }
  }

  async submitPendingText(turn) {
    try {
      this.callbacks.onState('Thinking…')
      if (this.interruptPromise) await this.interruptPromise.catch(() => undefined)
      if (this.closed || turn.generation !== this.turnGeneration) return
      this.baselineAssistantId = this.controller.latestAssistant()?.id || null
      this.waitingForAssistant = true
      this.activeTurnGeneration = turn.generation
      if (!this.controller.submitText(turn.text)) {
        this.waitingForAssistant = false
        this.activeTurnGeneration = null
      }
    } catch (error) {
      if (turn.generation === this.activeTurnGeneration) {
        this.waitingForAssistant = false
        this.activeTurnGeneration = null
      }
      this.callbacks.onError(`Hermes turn failed: ${errorText(error)}`)
    }
  }

  handleAssistantResponse(response) {
    if (this.closed || !this.waitingForAssistant || !response || response.pending || !response.text) return
    if (this.activeTurnGeneration !== this.turnGeneration) return
    if (response.id && (response.id === this.baselineAssistantId || response.id === this.lastSpokenAssistantId)) return
    this.waitingForAssistant = false
    this.activeTurnGeneration = null
    if (response.id) this.lastSpokenAssistantId = response.id
    this.speak(response.text, response.id)
  }

  speak(text, correlation) {
    if (!this.send({
      type: 'response.create', event_id: `desktop-talk-${crypto.randomUUID()}`,
      response: {
        conversation: 'none', metadata: { correlation },
        input: [{ type: 'message', role: 'user', content: [{ type: 'input_text', text }] }],
        instructions: RENDER_INSTRUCTIONS, output_modalities: ['audio'],
        audio: { output: { voice: this.session.voice } }, tools: [], tool_choice: 'none'
      }
    })) this.callbacks.onError('Realtime event channel is not open.')
  }
}

function TalkPrimary({ disabled }) {
  const controller = useComposerVoiceController()
  const state = useValue($talk)
  const active = state.phase === 'active'
  const starting = state.phase === 'starting'
  const Icon = starting ? icons.Loader2 : active ? icons.Square : icons.AudioLines
  return jsxs(Fragment, { children: [
    active && jsx(Tip, { label: state.muted ? 'Unmute Realtime Talk' : 'Mute Realtime Talk', children: jsx(Button, {
      'aria-label': state.muted ? 'Unmute Realtime Talk' : 'Mute Realtime Talk', className: 'size-(--control-size) shrink-0', disabled,
      onClick: () => { haptic('open'); toggleMute() }, size: 'icon', type: 'button', variant: state.muted ? 'secondary' : 'ghost',
      children: jsx(state.muted ? icons.MicOff : icons.Mic, { className: 'size-3.5' })
    }) }),
    jsx(Tip, { label: active ? `Stop Realtime Talk — ${state.status}` : state.error || 'Start Realtime Talk', children: jsx(Button, {
      'aria-label': active ? 'Stop Realtime Talk' : 'Start Realtime Talk', className: cn('size-(--control-size) shrink-0', state.phase === 'error' && 'text-destructive'), disabled: disabled || starting,
      onClick: () => { haptic('open'); if (active) stopTalk(); else void startTalk(controller) }, size: 'icon', type: 'button',
      children: jsx(Icon, { className: cn('size-3.5', starting && 'animate-spin', active && 'fill-current') })
    }) })
  ] })
}

const plugin = {
  id: 'hermes-talk', name: 'Hermes Realtime Talk',
  description: 'Canonical Hermes conversations with local full-duplex Realtime audio.', defaultEnabled: false,
  register(ctx) {
    pluginContext = ctx
    ctx.register({
      id: 'realtime-talk', area: 'composer.actions', title: 'Realtime Talk', order: 10,
      data: { label: 'Realtime Talk', start: startTalk, render: props => jsx(TalkPrimary, props) }
    })
    ctx.onDispose(() => { stopTalk(); pluginContext = null })
  }
}

export { DesktopTalkTransport }
export default plugin
