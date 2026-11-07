draft: not reviewed

# ragleap-physical

**Estado: pre-alpha. Solo simulación. Nada de lo que hay aquí controla hardware real.**

Una pequeña biblioteca que conecta a los empleados de RagLeap IA con simuladores de código abierto, permitiendo realizar tareas de desarrollo y pruebas de manera segura. La observación es lo primero. Cada acción pasa por un proceso de propuesta, aprobación y un marco de seguridad, integrado en el propio puente de conexión, que centraliza el control. El modelo no tiene capacidad para modificar dichos límites.

## Qué funciona actualmente (y qué no)

- Formatos de observación y propuesta (v0), los cuatro niveles de capacidad, el marco de seguridad, aprobaciones vinculadas a la propuesta específica, simulación de prueba (*dry run*), verificación de resultados y una función de parada que no requiere aprobación. Probado con un simulador integrado muy sencillo de una sola articulación (que utiliza únicamente la biblioteca estándar).
- Exactamente cuatro herramientas para un agente de IA: listar fuentes, obtener una observación, obtener el sobre y proponer una acción.
  Ninguna de ellas permite aprobar, aplicar, detener o modificar un límite. Estas herramientas se ofrecen a través de un servidor MCP ligero (basado en la biblioteca estándar, con *loopback* por defecto y un *bearer token* opcional). Se ha verificado su funcionamiento con el SDK oficial de MCP para Python (`mcp>=1.28,<2`).
  El cliente MCP propio de RagLeap no se ha revisado ni probado; es posible que requiera ajustes (por ejemplo, para soportar HTTPS).
- Adaptadores de MuJoCo como complemento opcional: uno solo de observación y otro tipo servo que sigue la instrucción `set_position_target` en la simulación, exclusivamente a través del puente.
- Límites del espacio de trabajo: un volumen definido en x, y, z dentro del cual debe permanecer cada punto rastreado. Esta restricción se verifica en el estado inicial, durante una ejecución de prueba y, posteriormente, a lo largo del movimiento ejecutado mediante intervalos breves. Si se establece un espacio de trabajo y el adaptador no puede proporcionar las posiciones, se rechaza cualquier operación. Un modelo de brazo de dos eslabones (implementado tanto de forma nativa como mediante MuJoCo) permite poner a prueba esta funcionalidad.
- Una herramienta de máquina simulada (desgaste de la herramienta): la IA solo puede ajustar el desplazamiento de la herramienta, en micrómetros, dentro de unos límites determinados (rango, tamaño de paso, intervalo mínimo entre cambios). El cambio de la herramienta es una acción humana y no un comando. Se trata de un modelo con valores ficticios que no ofrece información sobre una máquina real.
- Reforzado tras una revisión independiente realizada por otras dos sesiones de IA (consultar el CHANGELOG): comprobaciones más estrictas de las entradas y predicciones, y eliminación de la autoaprobación.
- Un modelo de brazo de tres articulaciones (archivo XML de MuJoCo en `examples/robots/`, además de un componente sustituto integrado), una simulación de observación para ROS 2 que funciona sin conexión y no requiere la instalación de ROS, y un experimento de repetibilidad en `benchmarks/` (reproducción exacta dentro de un mismo entorno instalado; no se garantiza la repetibilidad entre distintas máquinas).
- Una consola de operador (`ragleap_physical.console`): la parte humana del puente. Una persona identificada visualiza las propuestas pendientes, aprueba el resumen (digest) específico, ejecuta la acción y tiene la capacidad de detener el proceso. Funciona exclusivamente mediante una conexión local(loopback), con un único token por persona, y el nombre de quien aprueba se extrae del token, nunca de la solicitud. No es una herramienta: la IA no tiene acceso a ella.
- Dos simulaciones industriales de solo lectura, sin conexión a la red y sin medidores ni máquinas reales: un entorno energético simulado y un lector de documentos MTConnect con restricciones estrictas (rechaza DTDs y entidades, además de limitar el tamaño). El estándar MTConnect en sí no es exclusivamente de solo lectura; sin embargo, estos adaptadores funcionan únicamente en modo de lectura. Consulta `docs/industry/observe-only-adapters.md` y la guía de primeros pasos `docs/first-hour.md`.
- Aún no implementado: un segundo simulador, hardware real y clientes Java y npm.
- La simulación no es la realidad. No interpretes que superar una prueba de simulación demuestra que el sistema funcionará en una máquina real.


## Fuera del alcance, siempre


Sistemas de seguridad vital, dispositivos médicos, vehículos en vías públicas, armas y cualquier elemento que pueda causar daño a las personas, sin contar con un mecanismo de seguridad de hardware independiente que detenga su funcionamiento.


## Documentos


- Plan, decisiones, normas de seguridad y convenciones: [docs/HANDOVER.md](docs/HANDOVER.md)
- Diseño (formatos, marco de seguridad, modelo de amenazas): [docs/design/phase0-interface.md](docs/design/phase0-interface.md)


## Pruébalo


```
pip install -e ".[test]"          # solo el núcleo, biblioteca estándar
pip install -e ".[mujoco,test]"   # con el adaptador MuJoCo opcional
pip install -e ".[test,interop]"  # también ejecuta la prueba de interoperabilidad del SDK de MCP
pytest
python examples/demo_fake.py
python examples/two_link_workspace_demo.py
```


Informa de vulnerabilidades de forma privada a través de la pestaña de seguridad del repositorio (consulta `SECURITY.md`).


## Otros idiomas (borradores, no revisados ​​por hablantes nativos)
[Hindi](README.hi.md) · [Tamil](README.ta.md)

