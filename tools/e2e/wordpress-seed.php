<?php
/**
 * What tools/e2e/wordpress.mjs expects to find in the test WordPress: three
 * people and, where the course plugins are active, two courses with one of
 * the people enrolled. Run it once, with wp-cli:
 *
 *     wp eval-file wordpress-seed.php
 *
 * It can be run again: what is already there is kept.
 *
 * - ana   a student, enrolled in "Matemática 1" (Tutor LMS) and
 *         "História 1" (LifterLMS)
 * - rui   an editor: one of the roles that moderate
 * - beto  a student of no course
 *
 * All three with the password in E2E_WP_PASSWORD (Aluno-pass-123! otherwise).
 */

$qqplay_password = getenv( 'E2E_WP_PASSWORD' ) ? getenv( 'E2E_WP_PASSWORD' ) : 'Aluno-pass-123!';
$qqplay_people   = array(
	'ana'  => array( 'Ana', 'Souza', 'subscriber' ),
	'rui'  => array( 'Rui', 'Lima', 'editor' ),
	'beto' => array( 'Beto', 'Dias', 'subscriber' ),
);
foreach ( $qqplay_people as $qqplay_login => $qqplay_person ) {
	if ( ! get_user_by( 'login', $qqplay_login ) ) {
		wp_insert_user(
			array(
				'user_login'   => $qqplay_login,
				'user_pass'    => $qqplay_password,
				'user_email'   => $qqplay_login . '@example.com',
				'first_name'   => $qqplay_person[0],
				'last_name'    => $qqplay_person[1],
				'display_name' => $qqplay_person[0] . ' ' . $qqplay_person[1],
				'role'         => $qqplay_person[2],
			)
		);
	}
	echo "user $qqplay_login\n";
}
$qqplay_ana = get_user_by( 'login', 'ana' );

/**
 * A published course by its slug, made if it is not there.
 *
 * @param string $type  The post type of the course plugin.
 * @param string $title Its title.
 * @param string $slug  The end of its address: the class.
 * @return int
 */
function qqplay_e2e_course( $type, $title, $slug ) {
	$found = get_posts(
		array(
			'post_type'   => $type,
			'name'        => $slug,
			'post_status' => 'publish',
			'numberposts' => 1,
		)
	);
	if ( $found ) {
		return (int) $found[0]->ID;
	}
	return (int) wp_insert_post(
		array(
			'post_type'   => $type,
			'post_title'  => $title,
			'post_name'   => $slug,
			'post_status' => 'publish',
			'post_author' => 1,
		)
	);
}

if ( function_exists( 'tutor_utils' ) ) {
	$qqplay_course = qqplay_e2e_course( tutor()->course_post_type, 'Matemática 1', 'matematica-1' );
	if ( ! in_array( $qqplay_course, array_map( 'intval', (array) tutor_utils()->get_enrolled_courses_ids_by_user( $qqplay_ana->ID ) ), true ) ) {
		$qqplay_enrolment = tutor_utils()->do_enroll( $qqplay_course, 0, $qqplay_ana->ID );
		if ( $qqplay_enrolment ) {
			wp_update_post(
				array(
					'ID'          => $qqplay_enrolment,
					'post_status' => 'completed',
				)
			);
		}
	}
	echo "Tutor LMS course matematica-1\n";
} else {
	echo "Tutor LMS is not active: no matematica-1 course\n";
}

if ( function_exists( 'llms_enroll_student' ) ) {
	$qqplay_course = qqplay_e2e_course( 'course', 'História 1', 'historia-1' );
	llms_enroll_student( $qqplay_ana->ID, $qqplay_course, 'admin' );
	echo "LifterLMS course historia-1\n";
} else {
	echo "LifterLMS is not active: no historia-1 course\n";
}

if ( function_exists( 'qqplay_user_classes' ) ) {
	echo 'classes of ana: ' . implode( ', ', qqplay_user_classes( $qqplay_ana ) ) . "\n";
} else {
	echo "The QQplay plugin is not active.\n";
}
