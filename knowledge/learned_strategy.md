# Estrategia Aprendida - Estado Inicial

## Análisis de Rendimiento
- **Estado Actual:** Fase de arranque/calibración. No se han registrado conversiones ni rechazos directos.
- **Diagnóstico:** El sistema se encuentra en un estado de "línea base". No hay evidencia empírica de saturación ni de ángulos ganadores.

## Directrices para el Orquestador (Agente 1)

### 1. Nichos a Priorizar (Exploración)
Dado que no hay datos de fracaso, se debe proceder con una **exploración diversificada** basada estrictamente en el `rag/jofra_market_strategy.md`. Priorizar sectores con alta densidad de leads en Apollo y necesidades claras de digitalización/automatización.

### 2. Ángulos de Abordaje (Tácticas)
- **Enfoque:** Implementar el "Ángulo de Eficiencia Operativa".
- **Acción:** El Agente 3 debe centrarse en promesas de ahorro de tiempo y reducción de errores manuales, ya que es la propuesta de valor más genérica y segura para iniciar la recolección de datos.

### 3. Restricciones y Alertas (Qué evitar)
- **Evitar:** No concentrar todo el volumen de envío en un solo sub-nicho durante las próximas 48 horas para evitar el bloqueo de dominios y permitir una muestra estadística representativa.
- **Alerta:** Si el volumen de envíos es alto pero el número de respuestas es 0, se deberá revisar el filtro de calificación del Agente 2 (Enricher).

## Configuración de Próximo Ciclo
- **Modo:** Exploración Agresiva.
- **KPI Objetivo:** Obtener las primeras respuestas (positivas o negativas) para alimentar la base de datos de experiencia (+1/-1).