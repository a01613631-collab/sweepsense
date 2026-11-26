// Única puerta de la app hacia los datos.
// - Modo "demo": usa el robot simulado (demo.js), no necesita nada conectado.
// - Modo "robot": habla por HTTP con la API de la Raspberry Pi (servidor/app.py).
// Si algún día cambian de plataforma (por ejemplo Blynk), solo se cambia este archivo.
import AsyncStorage from '@react-native-async-storage/async-storage';
import * as demo from './demo';

const CLAVE = 'sweepsense.config';
let config = { modo: 'demo', url: 'http://10.42.0.1:5000' };
const oyentes = new Set();

export async function cargarConfig() {
  try {
    const guardada = await AsyncStorage.getItem(CLAVE);
    if (guardada) config = { ...config, ...JSON.parse(guardada) };
  } catch {}
  oyentes.forEach((f) => f(config));
  return config;
}

export function leerConfig() {
  return config;
}

export async function guardarConfig(nueva) {
  config = { ...config, ...nueva };
  try { await AsyncStorage.setItem(CLAVE, JSON.stringify(config)); } catch {}
  oyentes.forEach((f) => f(config));
}

export function alCambiarConfig(f) {
  oyentes.add(f);
  return () => oyentes.delete(f);
}

const ESPERA_MS = 5000; // si la Pi no contesta en este tiempo, se da por caída

async function http(ruta, opciones = {}) {
  const base = config.url.replace(/\/+$/, '');
  const control = new AbortController();
  const reloj = setTimeout(() => control.abort(), ESPERA_MS);
  try {
    const r = await fetch(`${base}/api${ruta}`, {
      ...opciones,
      headers: { 'Content-Type': 'application/json', ...(opciones.headers || {}) },
      signal: control.signal,
    });
    const datos = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(datos.error || `Error ${r.status}`);
    return datos;
  } catch (e) {
    if (e.name === 'AbortError') throw new Error(`La Raspberry Pi no responde en ${base}`);
    // fetch falla con TypeError ("Network request failed") cuando no hay red o la IP no existe
    if (e instanceof TypeError) throw new Error(`No hay conexión con ${base}. ¿El celular está en la red Roomba?`);
    throw e;
  } finally {
    clearTimeout(reloj);
  }
}

const esDemo = () => config.modo === 'demo';

export const api = {
  estado: () => (esDemo() ? Promise.resolve(demo.estado()) : http('/estado')),
  mapa: (sesionId) => (esDemo() ? Promise.resolve(demo.mapa(sesionId)) : http(sesionId ? `/mapa?sesion=${sesionId}` : '/mapa')),
  sesiones: () => (esDemo() ? Promise.resolve(demo.sesiones()) : http('/sesiones')),
  tareas: () => (esDemo() ? Promise.resolve(demo.tareas()) : http('/tareas')),
  eventos: (limite = 20) => (esDemo() ? Promise.resolve(demo.eventos(limite)) : http(`/eventos?limite=${limite}`)),
  comando: (cuerpo) =>
    esDemo()
      ? new Promise((ok, mal) => { try { ok(demo.comando(cuerpo)); } catch (e) { mal(e); } })
      : http('/comando', { method: 'POST', body: JSON.stringify(cuerpo) }),
  // Guardar una limpieza para después (queda en "Pendientes")
  crearTarea: (cuerpo) =>
    esDemo() ? Promise.resolve(demo.crearTarea(cuerpo)) : http('/tareas', { method: 'POST', body: JSON.stringify(cuerpo) }),
  // Historial de lecturas para las gráficas (batería, voltaje, corriente)
  lecturas: (minutos = 30) => (esDemo() ? Promise.resolve(demo.lecturas(minutos)) : http(`/lecturas?minutos=${minutos}&puntos=120`)),
  // Solo existe en el robot simulado; con el robot real no hace nada
  simularFuga: (v) => { if (esDemo()) demo.simularFuga(v); },
};
