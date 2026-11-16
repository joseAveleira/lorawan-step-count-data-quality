# Exportación y validación FHIR

Generado por `software/fhir/export_validate.py` el 2026-10-24T10:13:26.

21 pruebas con bins; un Bundle tras cada trama recibida con las versiones nuevas o cambiadas.

| id | strategy | bundles | intervals | observation_versions | provenance | patient_device | observed_zero | observed_positive | lost | pending | received_late |
|---|---|---|---|---|---|---|---|---|---|---|---|
| C1_S1 | 1 | 48 | 192 | 192 | 48 | 96 | 134 | 58 | 0 | 0 | 0 |
| C1_S2 | 2 | 48 | 192 | 192 | 48 | 96 | 134 | 58 | 0 | 0 | 0 |
| C1_S3 | 3 | 48 | 192 | 192 | 48 | 96 | 134 | 58 | 0 | 0 | 0 |
| C2_S1 | 1 | 16 | 328 | 328 | 16 | 32 | 46 | 18 | 264 | 0 | 0 |
| C2_S2 | 2 | 15 | 324 | 324 | 15 | 30 | 46 | 26 | 252 | 0 | 0 |
| C2_S3 | 3 | 11 | 328 | 592 | 11 | 22 | 232 | 96 | 0 | 264 | 264 |
| C3-10_S1 | 1 | 50 | 220 | 220 | 50 | 100 | 163 | 37 | 20 | 0 | 0 |
| C3-10_S2 | 2 | 50 | 220 | 220 | 50 | 100 | 167 | 41 | 12 | 0 | 0 |
| C3-10_S3 | 3 | 48 | 220 | 240 | 48 | 96 | 169 | 51 | 0 | 20 | 20 |
| C3-30_S1 | 1 | 40 | 220 | 220 | 40 | 80 | 100 | 60 | 60 | 0 | 0 |
| C3-30_S2 | 2 | 40 | 220 | 220 | 40 | 80 | 114 | 66 | 40 | 0 | 0 |
| C3-30_S3 | 3 | 37 | 220 | 288 | 37 | 74 | 142 | 78 | 0 | 68 | 68 |
| C4-24_S1 | 1 | 31 | 220 | 220 | 31 | 62 | 82 | 42 | 96 | 0 | 0 |
| C4-24_S2 | 2 | 31 | 220 | 220 | 31 | 62 | 86 | 42 | 92 | 0 | 0 |
| C4-24_S3 | 3 | 26 | 220 | 316 | 26 | 52 | 152 | 68 | 0 | 96 | 96 |
| C4-48_S3 | 3 | 22 | 316 | 508 | 22 | 44 | 221 | 95 | 0 | 192 | 192 |
| C4-72_S3 | 3 | 16 | 412 | 700 | 16 | 32 | 288 | 124 | 0 | 288 | 288 |
| C5-overflow_S3 | 3 | 26 | 412 | 504 | 26 | 52 | 141 | 71 | 200 | 92 | 88 |
| C5-reboot_S3 | 3 | 26 | 220 | 220 | 26 | 52 | 152 | 68 | 0 | 0 | 0 |
| REAL-C4-24_S1 | 1 | 31 | 220 | 220 | 31 | 62 | 82 | 42 | 96 | 0 | 0 |
| REAL-C4-24_S3 | 3 | 26 | 220 | 316 | 26 | 52 | 152 | 68 | 0 | 96 | 96 |

Totales: bundles 686, intervals 5336, observation_versions 6452, provenance 686, patient_device 1372, observed_zero 2937, observed_positive 1267, lost 1132, pending 1116, received_late 1112, resources 8510

Validador de HL7 7.0.3 contra FHIR R4 (4.0.1): código de salida 0, 49.7 s.

Ficheros validados: 686; con errores: 0; mensajes: {'warning': 19166}

## warning
- 8510 x Constraint failed: dom-#: 'A resource should have narrative for robust management' (defined in http://hl#.org/fhir/StructureDefinition/DomainResource) (Best Practice Recommendation)
- 6452 x Best Practice Recommendation: In general, all observations should have a performer
- 4204 x UCUM Codes that contain human readable annotations like {steps} can be misleading (e.g. they are ignored when comparing units). Best Practice is not to depend on annotations in the UCUM code, so this usage should be checked
